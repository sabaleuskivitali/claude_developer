using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Browser;

/// <summary>
/// Reads browser events from browser_queue.jsonl written by the native messaging host
/// (NativeMessagingDetector.RunAsync) and inserts them into the main EventStore.
///
/// Runs in the main service process — no SQLite contention.
/// Polls every 2 seconds; processes all pending lines, then truncates the file.
/// </summary>
public sealed class BrowserQueueImporter : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<BrowserQueueImporter> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition      = JsonIgnoreCondition.WhenWritingNull,
    };

    private static readonly string QueueFile = Path.Combine(
        Path.GetTempPath(), "WinDiagBrowserQueue.jsonl");

    public BrowserQueueImporter(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<BrowserQueueImporter> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(2));

        while (await timer.WaitForNextTickAsync(ct))
        {
            try { ProcessQueue(); }
            catch (Exception ex)
            {
                _logger.LogDebug("BrowserQueueImporter: {Msg}", ex.Message);
            }
        }
    }

    private void ProcessQueue()
    {
        if (!File.Exists(QueueFile)) return;

        string[] lines;
        try
        {
            // Read all lines, then truncate — minimise window where native host
            // and importer both touch the file
            lines = File.ReadAllLines(QueueFile);
            File.WriteAllText(QueueFile, string.Empty);
        }
        catch { return; }

        var imported = 0;
        foreach (var line in lines)
        {
            var trimmed = line.Trim();
            if (string.IsNullOrEmpty(trimmed)) continue;

            try
            {
                var msg = JsonSerializer.Deserialize<BrowserMessage>(trimmed, _jsonOpts);
                if (msg != null)
                {
                    StoreEvent(msg);
                    imported++;
                }
            }
            catch (JsonException) { /* malformed line — skip */ }
        }

        if (imported > 0)
            _logger.LogDebug("BrowserQueueImporter: imported {N} browser events", imported);
    }

    private void StoreEvent(BrowserMessage msg)
    {
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
        "pageLoad"     => nameof(EventType.BrowserPageLoad),
        "navigation"   => nameof(EventType.BrowserNavigation),
        "tabActivated" => nameof(EventType.BrowserTabActivated),
        "fieldFocus"   => nameof(EventType.BrowserFormFieldFocus),
        "fieldBlur"    => nameof(EventType.BrowserFormFieldBlur),
        "elementClick" => nameof(EventType.BrowserElementClick),
        "xhrRequest"   => nameof(EventType.BrowserXhrRequest),
        "formSubmit"   => nameof(EventType.BrowserFormSubmit),
        _              => nameof(EventType.BrowserNavigation),
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
            var parts = uri.Query.TrimStart('?').Split('&')
                .Where(p => !_sensitiveParams.Any(s =>
                    p.Split('=')[0].ToLowerInvariant().Contains(s)));
            var builder = new UriBuilder(uri) { Query = string.Join("&", parts) };
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
