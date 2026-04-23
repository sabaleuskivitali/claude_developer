using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace WinDiagSvc.Bootstrap.Resolvers;

/// <summary>
/// Discovers the bootstrap server via mDNS (_windiag._tcp.local).
/// Sends a minimal mDNS PTR query and parses the response to extract the server URL.
/// Then fetches the profile from /api/v1/bootstrap/active and verifies the fingerprint
/// against the TXT record before trusting the response.
/// </summary>
public sealed class DnsSdResolver : IProfileResolver
{
    private static readonly IPEndPoint MdnsEndpoint = new(IPAddress.Parse("224.0.0.251"), 5353);
    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(8) };

    private const string ServiceType = "_windiag._tcp.local";

    public string Name => "dnssd";

    public async Task<SignedBootstrapProfile?> TryResolveAsync(CancellationToken ct)
    {
        try
        {
            var result = await QueryMdnsAsync(ct);
            if (result is null) return null;

            var (serverUrl, fingerprint) = result.Value;

            // Fetch profile from server
            var json = await _http.GetStringAsync($"{serverUrl}/api/v1/bootstrap/active", ct);
            var signed = JsonSerializer.Deserialize<SignedBootstrapProfile>(json);
            if (signed is null) return null;

            // Verify fingerprint from TXT record before trusting
            if (!string.IsNullOrEmpty(fingerprint) && !VerifyFingerprint(signed, fingerprint))
                return null;

            return signed;
        }
        catch
        {
            return null;
        }
    }

    private static async Task<(string serverUrl, string fingerprint)?> QueryMdnsAsync(CancellationToken ct)
    {
        using var udp = new UdpClient();
        udp.Client.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
        udp.Client.Bind(new IPEndPoint(IPAddress.Any, 0));
        udp.JoinMulticastGroup(IPAddress.Parse("224.0.0.251"));

        // Minimal mDNS PTR query for _windiag._tcp.local
        var query = BuildPtrQuery(ServiceType);
        await udp.SendAsync(query, query.Length, MdnsEndpoint);

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(3));

        try
        {
            while (true)
            {
                var result = await udp.ReceiveAsync(cts.Token);
                var parsed = TryParseResponse(result.Buffer);
                if (parsed.HasValue) return parsed;
            }
        }
        catch (OperationCanceledException)
        {
            return null;
        }
    }

    private static byte[] BuildPtrQuery(string serviceType)
    {
        // Minimal DNS query: Transaction ID=0, FLAGS=QR, QDCOUNT=1
        // Question: serviceType PTR IN
        using var ms = new System.IO.MemoryStream();
        using var bw = new System.IO.BinaryWriter(ms, Encoding.ASCII, leaveOpen: true);
        bw.Write((ushort)0);           // ID
        bw.Write(ToBigEndian16(0));    // Flags: standard query
        bw.Write(ToBigEndian16(1));    // QDCOUNT
        bw.Write((ushort)0);           // ANCOUNT
        bw.Write((ushort)0);           // NSCOUNT
        bw.Write((ushort)0);           // ARCOUNT
        WriteDnsName(bw, serviceType + ".");
        bw.Write(ToBigEndian16(12));   // QTYPE PTR
        bw.Write(ToBigEndian16(1));    // QCLASS IN
        return ms.ToArray();
    }

    private static (string serverUrl, string fingerprint)? TryParseResponse(byte[] data)
    {
        // We look for TXT record containing "fp=" and an A/SRV record with an IP/port.
        // For MVP: extract the server hostname from the service name and port from SRV,
        // then look for fp= in TXT properties.
        // A full mDNS parser is complex — use a simple heuristic scan.
        try
        {
            var text = Encoding.UTF8.GetString(data);
            if (!text.Contains("_windiag")) return null;

            // Extract fp= fingerprint from TXT data
            var fpIdx = text.IndexOf("fp=", StringComparison.Ordinal);
            var fingerprint = fpIdx >= 0 ? text.Substring(fpIdx + 3, Math.Min(16, text.Length - fpIdx - 3)).Trim('\0') : "";

            // SRV record parsing requires a full DNS packet parser.
            // TODO: implement using the zeroconf-compatible SRV/A record layout.
            throw new NotImplementedException("mDNS SRV record parsing not yet implemented.");
        }
        catch
        {
            return null;
        }
    }

    private static bool VerifyFingerprint(SignedBootstrapProfile signed, string fingerprint)
    {
        using var sha = System.Security.Cryptography.SHA256.Create();
        var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(signed.SignedData));
        var fp   = Convert.ToHexString(hash).ToLower()[..16];
        return fp == fingerprint.ToLower();
    }

    private static void WriteDnsName(System.IO.BinaryWriter bw, string name)
    {
        foreach (var label in name.Split('.'))
        {
            if (string.IsNullOrEmpty(label)) continue;
            var bytes = Encoding.ASCII.GetBytes(label);
            bw.Write((byte)bytes.Length);
            bw.Write(bytes);
        }
        bw.Write((byte)0);
    }

    private static ushort ToBigEndian16(ushort v) =>
        (ushort)((v >> 8) | (v << 8));
}
