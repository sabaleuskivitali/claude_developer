using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;

namespace WinDiagSvc.Sync;

/// <summary>
/// Discovers the diag_api server URL.
/// Priority: mDNS (_windiag._tcp.local) → ENV:WINDIAG_SERVER_URL → appsettings.ServerUrl
/// Caches the result; rediscovers when MarkUnreachable() is called.
/// Exposes a shared HttpClient with self-signed cert support and optional thumbprint pinning.
/// </summary>
public sealed class ServerDiscovery : IDisposable
{
    private readonly AgentSettings _settings;
    private readonly ILogger<ServerDiscovery> _logger;
    private readonly HttpClient _http;

    private string? _cachedUrl;
    private readonly SemaphoreSlim _lock = new(1, 1);
    private DateTime _nextRediscovery = DateTime.MinValue;

    private const string MdnsService = "_windiag._tcp.local";
    private const string MdnsGroup   = "224.0.0.251";
    private const int    MdnsPort    = 5353;

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

            _cachedUrl = await DiscoverAsync(ct);
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
        _cachedUrl = null;
        _nextRediscovery = DateTime.MinValue;
    }

    private async Task<string?> DiscoverAsync(CancellationToken ct)
    {
        // 1. mDNS
        var mdns = await MdnsQueryAsync(ct);
        if (mdns.HasValue)
            return $"https://{mdns.Value.host}:{mdns.Value.port}";

        // 2. Environment variable
        var envUrl = Environment.GetEnvironmentVariable("WINDIAG_SERVER_URL");
        if (!string.IsNullOrEmpty(envUrl))
            return envUrl.TrimEnd('/');

        // 3. appsettings
        if (!string.IsNullOrEmpty(_settings.ServerUrl))
            return _settings.ServerUrl.TrimEnd('/');

        return null;
    }

    // ─── mDNS ────────────────────────────────────────────────────────────────

    private async Task<(string host, int port)?> MdnsQueryAsync(CancellationToken ct)
    {
        try
        {
            using var udp = new UdpClient();
            udp.Client.ReceiveTimeout = 3000;
            udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            udp.Client.Bind(new IPEndPoint(IPAddress.Any, 0));

            var query   = BuildPtrQuery(MdnsService);
            var target  = new IPEndPoint(IPAddress.Parse(MdnsGroup), MdnsPort);
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

        // Header: ID=1, FLAGS=0 (standard query), QDCOUNT=1
        packet[pos++] = 0x00; packet[pos++] = 0x01;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x01;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;
        packet[pos++] = 0x00; packet[pos++] = 0x00;

        Array.Copy(nameBytes, 0, packet, pos, nameBytes.Length);
        pos += nameBytes.Length;

        packet[pos++] = 0x00; packet[pos++] = 0x0C; // QTYPE = PTR
        packet[pos++] = 0x00; packet[pos++] = 0x01; // QCLASS = IN
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

    private static (string host, int port)? ParseMdnsResponse(byte[] data, string senderIp)
    {
        if (data.Length < 12) return null;

        var anCount = (data[6] << 8) | data[7];
        var nsCount = (data[8] << 8) | data[9];
        var arCount = (data[10] << 8) | data[11];
        var qdCount = (data[4] << 8) | data[5];

        var pos = 12;

        for (var i = 0; i < qdCount; i++)
        {
            SkipName(data, ref pos);
            pos += 4;
        }

        int?   port   = null;
        string? ip    = null;

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
            else if (type == 1 && rdLen == 4) // A record
                ip = $"{data[pos]}.{data[pos+1]}.{data[pos+2]}.{data[pos+3]}";

            pos += rdLen;
        }

        if (port.HasValue)
            return (ip ?? senderIp, port.Value);

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

    // ─── HttpClient ──────────────────────────────────────────────────────────

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

        var thumbprint = _settings.ServerThumbprint;
        if (string.IsNullOrEmpty(thumbprint)) return true; // not yet pinned — allow first connect

        var actual = Convert.ToHexString(SHA256.HashData(cert.GetRawCertData()))
            .Replace(":", "").ToUpperInvariant();
        var expected = thumbprint.Replace(":", "").ToUpperInvariant();
        return actual == expected;
    }

    public void Dispose()
    {
        _http.Dispose();
        _lock.Dispose();
    }
}
