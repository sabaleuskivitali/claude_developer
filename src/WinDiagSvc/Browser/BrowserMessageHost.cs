using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Capture;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Browser;

/// <summary>
/// Chrome / Edge Native Messaging host.
/// Reads 4-byte length-prefixed JSON messages from stdin, writes acks to stdout.
/// Each message from the extension becomes an ActivityEvent in the store.
/// </summary>
public sealed class BrowserMessageHost : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<BrowserMessageHost> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNameCaseInsensitive  = true,
        DefaultIgnoreCondition       = JsonIgnoreCondition.WhenWritingNull,
    };

    public BrowserMessageHost(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<BrowserMessageHost> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        try
        {
            var stdin  = Console.OpenStandardInput();
            var stdout = Console.OpenStandardOutput();
            var lenBuf = new byte[4];

            while (!ct.IsCancellationRequested)
            {
                // Native Messaging: 4-byte LE length prefix
                var read = await ReadExactAsync(stdin, lenBuf, ct);
                if (read == 0) break; // stdin closed — extension disconnected

                var msgLen = BitConverter.ToInt32(lenBuf, 0);
                if (msgLen is <= 0 or > 1_048_576) continue; // sanity: max 1 MB

                var msgBuf = new byte[msgLen];
                await ReadExactAsync(stdin, msgBuf, ct);

                try
                {
                    var msg = JsonSerializer.Deserialize<BrowserMessage>(msgBuf, _jsonOpts);
                    if (msg != null)
                        StoreEvent(msg);
                }
                catch (JsonException ex)
                {
                    _logger.LogDebug("BrowserHost: bad JSON — {Msg}", ex.Message);
                }

                // Ack: {"ok":true}
                var ack = "{\"ok\":true}"u8.ToArray();
                var ackLen = BitConverter.GetBytes(ack.Length);
                await stdout.WriteAsync(ackLen, ct);
                await stdout.WriteAsync(ack, ct);
                await stdout.FlushAsync(ct);
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }
    }

    private void StoreEvent(BrowserMessage msg)
    {
        // Sanitize URL: strip query params containing sensitive keywords
        var url     = SanitizeUrl(msg.Url);
        var urlPath = ExtractPath(url);

        var eventType = MapEventType(msg.Type);
        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

        _store.Insert(new ActivityEvent
        {
            SessionId        = _store.SessionId,
            MachineId        = _settings.MachineId,
            UserId           = _settings.UserId,
            TimestampUtc     = raw,
            SyncedTs         = _ntp.SyncedTs(raw),
            DriftMs          = _ntp.CurrentDriftMs,
            DriftRatePpm     = _ntp.DriftRatePpm,
            Layer            = "browser",
            EventType        = eventType,
            BrowserName      = msg.Browser,
            BrowserUrl       = url,
            BrowserUrlPath   = urlPath,
            BrowserPageTitle = msg.PageTitle,
            DomElementTag    = msg.ElementTag,
            DomElementId     = msg.ElementId,
            DomElementName   = msg.ElementName,
            DomElementLabel  = msg.ElementLabel,
            DomFormAction    = msg.FormAction,
            DomFormFieldCount = msg.FormFieldCount,
            XhrMethod        = msg.XhrMethod,
            XhrStatus        = msg.XhrStatus,
        });
    }

    private static string MapEventType(string? type) => type switch
    {
        "pageLoad"        => nameof(EventType.BrowserPageLoad),
        "navigation"      => nameof(EventType.BrowserNavigation),
        "tabActivated"    => nameof(EventType.BrowserTabActivated),
        "fieldFocus"      => nameof(EventType.BrowserFormFieldFocus),
        "fieldBlur"       => nameof(EventType.BrowserFormFieldBlur),
        "elementClick"    => nameof(EventType.BrowserElementClick),
        "xhrRequest"      => nameof(EventType.BrowserXhrRequest),
        "formSubmit"      => nameof(EventType.BrowserFormSubmit),
        _                 => nameof(EventType.BrowserNavigation),
    };

    private static readonly string[] _sensitiveParams =
        ["token", "key", "secret", "auth", "session", "password", "passwd", "access_token"];

    private static string? SanitizeUrl(string? url)
    {
        if (url is null) return null;
        try
        {
            var uri = new Uri(url);
            if (string.IsNullOrEmpty(uri.Query)) return url;

            var query = uri.Query.TrimStart('?');
            var parts = query.Split('&')
                .Where(p =>
                {
                    var key = p.Split('=')[0].ToLowerInvariant();
                    return !_sensitiveParams.Any(s => key.Contains(s));
                });

            var safeQuery = string.Join("&", parts);
            var builder   = new UriBuilder(uri) { Query = safeQuery };
            return builder.Uri.ToString();
        }
        catch { return url; }
    }

    private static string? ExtractPath(string? url)
    {
        if (url is null) return null;
        try { return new Uri(url).AbsolutePath; }
        catch { return null; }
    }

    private static async Task<int> ReadExactAsync(Stream stream, byte[] buffer, CancellationToken ct)
    {
        var total = 0;
        while (total < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(total), ct);
            if (read == 0) return total;
            total += read;
        }
        return total;
    }

    private void WriteLayerError(Exception ex) =>
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            SyncedTs     = _ntp.SyncedTs(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "browser",
            EventType    = nameof(EventType.LayerError),
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });

    // -----------------------------------------------------------------------
    // Message DTO
    // -----------------------------------------------------------------------

    private sealed class BrowserMessage
    {
        public string? Type           { get; set; }
        public string? Browser        { get; set; }
        public string? Url            { get; set; }
        public string? PageTitle      { get; set; }
        public string? ElementTag     { get; set; }
        public string? ElementId      { get; set; }
        public string? ElementName    { get; set; }
        public string? ElementLabel   { get; set; }
        public string? FormAction     { get; set; }
        public int     FormFieldCount { get; set; }
        public string? XhrMethod      { get; set; }
        public int     XhrStatus      { get; set; }
    }
}
