using System.Net;
using System.Net.Http;
using System.Net.Sockets;
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
/// Priority: mDNS (_windiag._tcp.local) → HTTP discovery (:49100) → appsettings.ServerUrl
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
        // 1. UDP beacon — cross-subnet, server broadcasts every 30s
        var beacon = await UdpBeaconListenAsync(ct);
        if (beacon.HasValue)
        {
            var url = $"https://{beacon.Value.host}:{beacon.Value.port}";
            return (url, beacon.Value.thumbprint);
        }

        // 2. mDNS — same subnet, returns url + thumbprint from TXT
        var mdns = await MdnsQueryAsync(ct);
        if (mdns.HasValue)
        {
            var url = $"https://{mdns.Value.host}:{mdns.Value.port}";
            return (url, mdns.Value.thumbprint);
        }

        // 3. HTTP discovery on fixed port 49100 — when server IP is reachable via gateway
        var httpResult = await HttpDiscoveryAsync(ct);
        if (httpResult.HasValue)
            return httpResult.Value;

        // 4. appsettings.ServerUrl
        if (!string.IsNullOrEmpty(_settings.ServerUrl))
            return (_settings.ServerUrl.TrimEnd('/'), _settings.ServerThumbprint);

        return (null, null);
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

    // ─── HTTP discovery fallback ─────────────────────────────────────────────

    private async Task<(string url, string? thumbprint)?> HttpDiscoveryAsync(CancellationToken ct)
    {
        var candidates = GetDiscoveryCandidates();
        foreach (var host in candidates)
        {
            try
            {
                using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                cts.CancelAfter(2000);

                using var plainHttp = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
                var json = await plainHttp.GetStringAsync(
                    $"http://{host}:{DiscoveryPort}/discovery", cts.Token);

                var doc = JsonDocument.Parse(json).RootElement;
                if (!doc.TryGetProperty("port", out var portEl)) continue;

                var port   = portEl.GetInt32();
                var thumb  = doc.TryGetProperty("thumbprint", out var tEl) ? tEl.GetString() : null;
                var url    = $"https://{host}:{port}";

                _logger.LogInformation("ServerDiscovery: found via HTTP discovery at {Host}", host);
                return (url, thumb);
            }
            catch { }
        }
        return null;
    }

    private IEnumerable<string> GetDiscoveryCandidates()
    {
        // Default gateway — server is typically co-located or near the gateway
        var gw = GetDefaultGateway();
        if (gw != null) yield return gw;
    }

    private static string? GetDefaultGateway()
    {
        try
        {
            foreach (var ni in System.Net.NetworkInformation.NetworkInterface.GetAllNetworkInterfaces())
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
