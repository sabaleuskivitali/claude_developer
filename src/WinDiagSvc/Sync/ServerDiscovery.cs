using System.Net;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Numerics;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;

namespace WinDiagSvc.Sync;

/// <summary>
/// Discovers the diag_api server URL and TLS thumbprint.
///
/// Priority (L3-transparent, no IP hardcoding):
///   1. UDP beacon      — server broadcasts to subnet every 30s (same L2 segment)
///   2. mDNS            — _windiag._tcp.local query (same L2 segment)
///   3. DNS SRV         — _windiag._tcp.{domain} — works across L3, IT adds one record
///   4. DNS hostname    — windiag.{domain} → probe :49100 (fallback if no SRV)
///   5. Subnet scan     — parallel probe all /24 hosts on :49100 (50 concurrent, ~5s)
///   6. HTTP :49100     — probe gateway (same-subnet fallback)
///   7. appsettings     — ServerUrl explicit override (last resort)
///
/// Caches the result; rediscovers when MarkUnreachable() is called.
/// </summary>
public sealed class ServerDiscovery : IDisposable
{
    private readonly AgentSettings _settings;
    private readonly ILogger<ServerDiscovery> _logger;
    private readonly HttpClient _http;

    private string? _cachedUrl;
    private string? _cachedThumbprint;
    private readonly SemaphoreSlim _lock = new(1, 1);
    private DateTime _nextRediscovery = DateTime.MinValue;

    private const string MdnsService   = "_windiag._tcp.local";
    private const string MdnsGroup     = "224.0.0.251";
    private const int    MdnsPort      = 5353;
    private const int    DiscoveryPort = 49100;
    private const int    BeaconPort    = 49101;

    public ServerDiscovery(IOptions<AgentSettings> options, ILogger<ServerDiscovery> logger)
    {
        _settings = options.Value;
        _logger   = logger;
        _http     = CreateHttpClient();
    }

    public HttpClient HttpClient => _http;

    public async Task<string?> GetServerUrlAsync(CancellationToken ct = default)
    {
        await _lock.WaitAsync(ct);
        try
        {
            if (_cachedUrl != null && DateTime.UtcNow < _nextRediscovery)
                return _cachedUrl;

            (_cachedUrl, _cachedThumbprint) = await DiscoverAsync(ct);
            _nextRediscovery = DateTime.UtcNow.AddMinutes(60);

            if (_cachedUrl != null)
                _logger.LogInformation("ServerDiscovery: found {Url}", _cachedUrl);
            else
                _logger.LogWarning("ServerDiscovery: server not found");

            return _cachedUrl;
        }
        finally { _lock.Release(); }
    }

    public void MarkUnreachable()
    {
        _cachedUrl        = null;
        _cachedThumbprint = null;
        _nextRediscovery  = DateTime.MinValue;
    }

    private async Task<(string? url, string? thumbprint)> DiscoverAsync(CancellationToken ct)
    {
        // 1. UDP beacon — same L2 segment, server broadcasts every 30s
        var beacon = await UdpBeaconListenAsync(ct);
        if (beacon.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via UDP beacon");
            return ($"https://{beacon.Value.host}:{beacon.Value.port}", beacon.Value.thumbprint);
        }

        // 2. mDNS — same L2 segment
        var mdns = await MdnsQueryAsync(ct);
        if (mdns.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via mDNS");
            return ($"https://{mdns.Value.host}:{mdns.Value.port}", mdns.Value.thumbprint);
        }

        // 3. DNS SRV: _windiag._tcp.{domain} — works across L3, IT adds one DNS record
        var srv = await DnsSrvDiscoveryAsync(ct);
        if (srv.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via DNS SRV");
            return ($"https://{srv.Value.host}:{srv.Value.port}", srv.Value.thumbprint);
        }

        // 4. DNS A: windiag.{domain} → probe :49100 — fallback if no SRV record
        var dnsA = await DnsADiscoveryAsync(ct);
        if (dnsA.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via DNS A record");
            return dnsA.Value;
        }

        // 5. Parallel subnet scan — probe all /24 hosts on :49100 concurrently
        //    Works across L3 as long as the subnet is routable. No infrastructure needed.
        var scan = await SubnetScanAsync(ct);
        if (scan.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via subnet scan");
            return ($"https://{scan.Value.host}:{scan.Value.port}", scan.Value.thumbprint);
        }

        // 6. HTTP :49100 on gateway — last same-subnet fallback
        var http = await HttpDiscoveryAsync(ct);
        if (http.HasValue)
        {
            _logger.LogInformation("ServerDiscovery: found via HTTP gateway probe");
            return http.Value;
        }

        // 7. Explicit ServerUrl in appsettings — manual override
        if (!string.IsNullOrEmpty(_settings.ServerUrl))
        {
            _logger.LogInformation("ServerDiscovery: using ServerUrl from config");
            return (_settings.ServerUrl.TrimEnd('/'), _settings.ServerThumbprint);
        }

        return (null, null);
    }

    // ─── Parallel subnet scan ────────────────────────────────────────────────
    // Probes all hosts in local /24 subnets concurrently (50 at a time).
    // Each host is checked for port 49100; responders are probed for /discovery.
    // Typical /24: ~5 seconds. Skips own IPs and broadcast addresses.

    private async Task<(string host, int port, string? thumbprint)?> SubnetScanAsync(CancellationToken ct)
    {
        var candidates = GetSubnetHosts();
        if (candidates.Count == 0) return null;

        _logger.LogDebug("ServerDiscovery: subnet scan — {Count} hosts", candidates.Count);

        using var found = new CancellationTokenSource();
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, found.Token);

        (string host, int port, string? thumbprint)? result = null;
        var sem = new SemaphoreSlim(50); // max 50 concurrent probes

        var tasks = candidates.Select(async ip =>
        {
            await sem.WaitAsync(linked.Token).ConfigureAwait(false);
            try
            {
                if (linked.Token.IsCancellationRequested) return;
                var probe = await ProbeHttpDiscovery(ip, linked.Token).ConfigureAwait(false);
                if (probe.HasValue)
                {
                    result = probe;
                    found.Cancel(); // stop all other probes
                }
            }
            catch (OperationCanceledException) { }
            finally { sem.Release(); }
        });

        try { await Task.WhenAll(tasks).ConfigureAwait(false); }
        catch (OperationCanceledException) { } // expected when found.Cancel() fires

        return result;
    }

    private static List<string> GetSubnetHosts()
    {
        var hosts = new List<string>();
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                if (ni.NetworkInterfaceType is NetworkInterfaceType.Loopback
                    or NetworkInterfaceType.Tunnel) continue;

                foreach (var addr in ni.GetIPProperties().UnicastAddresses)
                {
                    if (addr.Address.AddressFamily != AddressFamily.InterNetwork) continue;

                    var ip   = addr.Address.GetAddressBytes();
                    var mask = addr.IPv4Mask?.GetAddressBytes();
                    if (mask == null) continue;

                    // Only scan /24 and larger (smaller = too many hosts)
                    var prefixLen = mask.Sum(b => BitOperations.PopCount(b));
                    if (prefixLen < 24) continue;

                    var network = new byte[4];
                    for (var i = 0; i < 4; i++) network[i] = (byte)(ip[i] & mask[i]);

                    // Enumerate all host addresses in the subnet (skip network + broadcast)
                    for (var i = 1; i <= 254; i++)
                    {
                        var host = new byte[] { network[0], network[1], network[2], (byte)i };
                        var hostIp = new IPAddress(host).ToString();
                        if (hostIp == addr.Address.ToString()) continue; // skip own IP
                        hosts.Add(hostIp);
                    }
                }
            }
        }
        catch { }
        return hosts;
    }

    // ─── DNS SRV discovery ───────────────────────────────────────────────────
    // IT runs once on domain DNS server:
    //   Add-DnsServerResourceRecord -Srv -ZoneName "corp.local" `
    //     -Name "_windiag._tcp" -DomainName "server.corp.local" -Priority 10 `
    //     -Weight 10 -Port 49100
    // No IP in code. No IP in agent config.

    private async Task<(string host, int port, string? thumbprint)?> DnsSrvDiscoveryAsync(CancellationToken ct)
    {
        try
        {
            var domain = IPGlobalProperties.GetIPGlobalProperties().DomainName;
            if (string.IsNullOrEmpty(domain)) return null;

            var srvName = $"_windiag._tcp.{domain}";
            _logger.LogDebug("ServerDiscovery: querying DNS SRV {Name}", srvName);

            // Resolve SRV via system DNS — parse the raw DNS response
            var addresses = await Dns.GetHostAddressesAsync(srvName, ct).ConfigureAwait(false);
            // GetHostAddresses doesn't support SRV; use low-level UDP DNS query
            return await QueryDnsSrvAsync(srvName, domain, ct);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("DNS SRV discovery failed: {Msg}", ex.Message);
            return null;
        }
    }

    private async Task<(string host, int port, string? thumbprint)?> QueryDnsSrvAsync(
        string srvName, string domain, CancellationToken ct)
    {
        try
        {
            // Build DNS query for SRV record
            var dnsServers = GetDnsServers();
            if (dnsServers.Count == 0) return null;

            var query   = BuildDnsQuery(srvName, 33); // type 33 = SRV
            var dnsEp   = new IPEndPoint(IPAddress.Parse(dnsServers[0]), 53);

            using var udp = new UdpClient();
            udp.Client.ReceiveTimeout = 2000;
            await udp.SendAsync(query, query.Length, dnsEp).ConfigureAwait(false);

            using var cts2 = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts2.CancelAfter(2000);
            var result = await udp.ReceiveAsync(cts2.Token).ConfigureAwait(false);

            var parsed = ParseDnsSrvResponse(result.Buffer);
            if (parsed == null) return null;

            // Now probe that host:port via HTTP discovery to get thumbprint
            var httpResult = await ProbeHttpDiscovery(parsed.Value.host, ct);
            if (httpResult.HasValue) return httpResult;

            return (parsed.Value.host, parsed.Value.port, null);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("DNS SRV query failed: {Msg}", ex.Message);
            return null;
        }
    }

    private static (string host, int port)? ParseDnsSrvResponse(byte[] data)
    {
        if (data.Length < 12) return null;
        var anCount = (data[6] << 8) | data[7];
        var qdCount = (data[4] << 8) | data[5];
        var pos = 12;

        // Skip questions
        for (var i = 0; i < qdCount; i++) { SkipName(data, ref pos); pos += 4; }

        for (var i = 0; i < anCount && pos + 10 <= data.Length; i++)
        {
            SkipName(data, ref pos);
            if (pos + 10 > data.Length) break;
            var type  = (data[pos] << 8) | data[pos + 1];
            var rdLen = (data[pos + 8] << 8) | data[pos + 9];
            pos += 10;
            if (pos + rdLen > data.Length) break;

            if (type == 33 && rdLen >= 7) // SRV
            {
                var port = (data[pos + 4] << 8) | data[pos + 5];
                var targetPos = pos + 6;
                var host = ReadDnsName(data, ref targetPos);
                if (!string.IsNullOrEmpty(host))
                    return (host, port);
            }
            pos += rdLen;
        }
        return null;
    }

    private static string ReadDnsName(byte[] data, ref int pos)
    {
        var parts = new List<string>();
        var limit = 50; // prevent infinite loops on malformed data
        while (pos < data.Length && data[pos] != 0 && limit-- > 0)
        {
            if ((data[pos] & 0xC0) == 0xC0)
            {
                var ptr = ((data[pos] & 0x3F) << 8) | data[pos + 1];
                pos += 2;
                var ptrPos = ptr;
                parts.Add(ReadDnsName(data, ref ptrPos));
                return string.Join(".", parts);
            }
            var len = data[pos++];
            if (pos + len > data.Length) break;
            parts.Add(Encoding.ASCII.GetString(data, pos, len));
            pos += len;
        }
        if (pos < data.Length && data[pos] == 0) pos++;
        return string.Join(".", parts);
    }

    // ─── DNS A hostname discovery ────────────────────────────────────────────
    // IT runs once: Add-DnsServerResourceRecord -A -ZoneName "corp.local"
    //   -Name "windiag" -IPv4Address "10.8.20.150"
    // Agent resolves windiag.{domain} → probes :49100 for port + thumbprint

    private async Task<(string url, string? thumbprint)?> DnsADiscoveryAsync(CancellationToken ct)
    {
        try
        {
            var domain = IPGlobalProperties.GetIPGlobalProperties().DomainName;
            if (string.IsNullOrEmpty(domain)) return null;

            var hostname = $"windiag.{domain}";
            _logger.LogDebug("ServerDiscovery: resolving DNS A {Host}", hostname);

            var addresses = await Dns.GetHostAddressesAsync(hostname, ct).ConfigureAwait(false);
            foreach (var addr in addresses.Where(a => a.AddressFamily == AddressFamily.InterNetwork))
            {
                var result = await ProbeHttpDiscovery(addr.ToString(), ct);
                if (result.HasValue) return ($"https://{result.Value.host}:{result.Value.port}", result.Value.thumbprint);
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug("DNS A discovery failed: {Msg}", ex.Message);
        }
        return null;
    }

    // ─── Shared HTTP probe ───────────────────────────────────────────────────

    private async Task<(string host, int port, string? thumbprint)?> ProbeHttpDiscovery(
        string host, CancellationToken ct)
    {
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(2000);
            using var plain = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var json = await plain.GetStringAsync($"http://{host}:{DiscoveryPort}/discovery", cts.Token);
            var doc  = JsonDocument.Parse(json).RootElement;
            if (!doc.TryGetProperty("port", out var portEl)) return null;
            var port  = portEl.GetInt32();
            var thumb = doc.TryGetProperty("thumbprint", out var tEl) ? tEl.GetString() : null;
            return (host, port, thumb);
        }
        catch { return null; }
    }

    // ─── DNS helpers ─────────────────────────────────────────────────────────

    private static List<string> GetDnsServers()
    {
        var servers = new List<string>();
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                foreach (var dns in ni.GetIPProperties().DnsAddresses)
                    if (dns.AddressFamily == AddressFamily.InterNetwork)
                        servers.Add(dns.ToString());
            }
        }
        catch { }
        return servers;
    }

    private static byte[] BuildDnsQuery(string name, ushort type)
    {
        var labels    = name.Split('.');
        var nameBytes = EncodeDnsName(labels);
        var packet    = new byte[12 + nameBytes.Length + 4];
        var pos       = 0;

        packet[pos++] = 0x00; packet[pos++] = 0x01; // ID
        packet[pos++] = 0x01; packet[pos++] = 0x00; // flags: recursion desired
        packet[pos++] = 0x00; packet[pos++] = 0x01; // QDCOUNT=1
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;

        Array.Copy(nameBytes, 0, packet, pos, nameBytes.Length);
        pos += nameBytes.Length;

        packet[pos++] = (byte)(type >> 8); packet[pos++] = (byte)(type & 0xFF);
        packet[pos++] = 0x00; packet[pos]   = 0x01; // class IN
        return packet;
    }

    // ─── UDP beacon ──────────────────────────────────────────────────────────

    private async Task<(string host, int port, string? thumbprint)?> UdpBeaconListenAsync(CancellationToken ct)
    {
        try
        {
            using var udp = new UdpClient();
            udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            udp.Client.Bind(new IPEndPoint(IPAddress.Any, BeaconPort));
            udp.Client.ReceiveTimeout = 3000;

            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(3000);

            var result = await udp.ReceiveAsync(cts.Token);
            var json   = System.Text.Encoding.UTF8.GetString(result.Buffer);
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!root.TryGetProperty("port", out var portEl)) return null;

            var port   = portEl.GetInt32();
            var thumb  = root.TryGetProperty("thumbprint", out var tEl) ? tEl.GetString() : null;
            var host   = result.RemoteEndPoint.Address.ToString();

            _logger.LogInformation("ServerDiscovery: found via UDP beacon from {Host}:{Port}", host, port);
            return (host, port, thumb);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("UDP beacon listen: {Msg}", ex.Message);
        }
        return null;
    }

    // ─── mDNS ────────────────────────────────────────────────────────────────

    private async Task<(string host, int port, string? thumbprint)?> MdnsQueryAsync(CancellationToken ct)
    {
        try
        {
            using var udp = new UdpClient();
            udp.Client.ReceiveTimeout = 3000;
            udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            udp.Client.Bind(new IPEndPoint(IPAddress.Any, 0));

            var query  = BuildPtrQuery(MdnsService);
            var target = new IPEndPoint(IPAddress.Parse(MdnsGroup), MdnsPort);
            await udp.SendAsync(query, query.Length, target);

            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(3000);

            while (!cts.Token.IsCancellationRequested)
            {
                try
                {
                    var result = await udp.ReceiveAsync(cts.Token);
                    var parsed = ParseMdnsResponse(result.Buffer, result.RemoteEndPoint.Address.ToString());
                    if (parsed.HasValue) return parsed;
                }
                catch (OperationCanceledException) { break; }
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug("mDNS query failed: {Msg}", ex.Message);
        }
        return null;
    }

    private static byte[] BuildPtrQuery(string serviceName)
    {
        var labels    = serviceName.Split('.');
        var nameBytes = EncodeDnsName(labels);
        var packet    = new byte[12 + nameBytes.Length + 4];
        var pos       = 0;

        packet[pos++] = 0x00; packet[pos++] = 0x01;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x01;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;

        Array.Copy(nameBytes, 0, packet, pos, nameBytes.Length);
        pos += nameBytes.Length;

        packet[pos++] = 0x00; packet[pos++] = 0x0C; // PTR
        packet[pos++] = 0x00; packet[pos++] = 0x01; // IN
        return packet;
    }

    private static byte[] EncodeDnsName(string[] labels)
    {
        var bytes = new List<byte>();
        foreach (var label in labels)
        {
            bytes.Add((byte)label.Length);
            bytes.AddRange(Encoding.ASCII.GetBytes(label));
        }
        bytes.Add(0);
        return [.. bytes];
    }

    private static (string host, int port, string? thumbprint)? ParseMdnsResponse(byte[] data, string senderIp)
    {
        if (data.Length < 12) return null;

        var anCount = (data[6] << 8) | data[7];
        var nsCount = (data[8] << 8) | data[9];
        var arCount = (data[10] << 8) | data[11];
        var qdCount = (data[4] << 8) | data[5];
        var pos     = 12;

        for (var i = 0; i < qdCount; i++) { SkipName(data, ref pos); pos += 4; }

        int?    port       = null;
        string? ip         = null;
        string? thumbprint = null;

        var totalRR = anCount + nsCount + arCount;
        for (var i = 0; i < totalRR && pos + 10 <= data.Length; i++)
        {
            SkipName(data, ref pos);
            if (pos + 10 > data.Length) break;

            var type  = (data[pos] << 8) | data[pos + 1];
            var rdLen = (data[pos + 8] << 8) | data[pos + 9];
            pos += 10;

            if (pos + rdLen > data.Length) break;

            if (type == 33 && rdLen >= 7) // SRV
                port = (data[pos + 4] << 8) | data[pos + 5];
            else if (type == 1 && rdLen == 4) // A
                ip = $"{data[pos]}.{data[pos+1]}.{data[pos+2]}.{data[pos+3]}";
            else if (type == 16) // TXT
                thumbprint = ParseTxtThumbprint(data, pos, rdLen) ?? thumbprint;

            pos += rdLen;
        }

        if (port.HasValue)
            return (ip ?? senderIp, port.Value, thumbprint);

        return null;
    }

    private static string? ParseTxtThumbprint(byte[] data, int offset, int rdLen)
    {
        var end = offset + rdLen;
        var pos = offset;
        while (pos < end)
        {
            var len = data[pos++];
            if (pos + len > end) break;
            var kv = Encoding.UTF8.GetString(data, pos, len);
            if (kv.StartsWith("thumbprint=", StringComparison.OrdinalIgnoreCase))
                return kv["thumbprint=".Length..];
            pos += len;
        }
        return null;
    }

    private static void SkipName(byte[] data, ref int pos)
    {
        while (pos < data.Length)
        {
            if (data[pos] == 0) { pos++; return; }
            if ((data[pos] & 0xC0) == 0xC0) { pos += 2; return; }
            pos += data[pos] + 1;
        }
    }

    // ─── HTTP discovery on gateway — same-subnet fallback ───────────────────

    private async Task<(string url, string? thumbprint)?> HttpDiscoveryAsync(CancellationToken ct)
    {
        var gw = GetDefaultGateway();
        if (gw == null) return null;

        var result = await ProbeHttpDiscovery(gw, ct);
        if (result.HasValue)
            return ($"https://{result.Value.host}:{result.Value.port}", result.Value.thumbprint);

        return null;
    }

    private static string? GetDefaultGateway()
    {
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                var props = ni.GetIPProperties();
                foreach (var gw in props.GatewayAddresses)
                    if (gw.Address.AddressFamily == AddressFamily.InterNetwork)
                        return gw.Address.ToString();
            }
        }
        catch { }
        return null;
    }

    // ─── HttpClient (TLS pinning) ────────────────────────────────────────────

    private HttpClient CreateHttpClient()
    {
        var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = ValidateCertificate
        };
        return new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(30) };
    }

    private bool ValidateCertificate(
        HttpRequestMessage _,
        X509Certificate2? cert,
        X509Chain? __,
        System.Net.Security.SslPolicyErrors ___)
    {
        if (cert is null) return false;

        // Use thumbprint discovered at runtime if not in settings
        var expected = _cachedThumbprint ?? _settings.ServerThumbprint;
        if (string.IsNullOrEmpty(expected)) return true; // not yet pinned — allow first connect

        var actual = Convert.ToHexString(SHA256.HashData(cert.GetRawCertData()))
            .Replace(":", "").ToUpperInvariant();
        return actual == expected.Replace(":", "").ToUpperInvariant();
    }

    public void Dispose()
    {
        _http.Dispose();
        _lock.Dispose();
    }
}
