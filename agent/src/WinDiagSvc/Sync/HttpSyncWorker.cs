using System.Net.Http;
using System.Net.Http.Json;
using WinDiagSvc.Capture;
using WinDiagSvc.Management;
using System.Text.Json;
using System.Text.Json.Serialization;
using Dapper;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Sync;

/// <summary>
/// Replaces FileSyncWorker. POSTs pending events to /api/v1/events in batches.
/// Uploads screenshots to /api/v1/screenshots/{machine}/{date}/{event_id}.
/// sent: 0=pending, 1=sent, 2=failed (retry next cycle).
/// </summary>
public sealed class HttpSyncWorker : BackgroundService
{
    private readonly EventStore     _store;
    private readonly NtpSynchronizer _ntp;
    private readonly ServerDiscovery _discovery;
    private readonly ErrorReporter  _errors;
    private readonly AgentSettings  _settings;
    private readonly ILogger<HttpSyncWorker> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNamingPolicy        = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition      = JsonIgnoreCondition.WhenWritingNull,
        ReferenceHandler            = ReferenceHandler.IgnoreCycles,
    };

    private const int BatchSize = 100;

    public HttpSyncWorker(
        EventStore store,
        NtpSynchronizer ntp,
        ServerDiscovery discovery,
        ErrorReporter errors,
        IOptions<AgentSettings> options,
        ILogger<HttpSyncWorker> logger)
    {
        _store     = store;
        _ntp       = ntp;
        _discovery = discovery;
        _errors    = errors;
        _settings  = options.Value;
        _logger    = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var jitter = ServerDiscovery.GetStartupJitter(_settings.MachineId, _settings.MaxStartupJitterSeconds);
        _logger.LogInformation("HttpSyncWorker: startup jitter {Sec}s", (int)jitter.TotalSeconds);
        await Task.Delay(jitter, ct);

        var interval = TimeSpan.FromSeconds(_settings.SyncIntervalSeconds);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await SyncAsync(ct); }
            catch (Exception ex) { _logger.LogWarning("HttpSyncWorker: {Msg}", ex.Message); }
        }
    }

    private async Task SyncAsync(CancellationToken ct)
    {
        var url = await _discovery.GetServerUrlAsync(ct);
        if (url is null) return;

        var sent    = 0;
        var failed  = 0;

        while (true)
        {
            var events = _store.ReadPending(BatchSize);
            if (events.Count == 0) break;

            // Upload screenshots before sending events (so path is valid on server)
            foreach (var ev in events)
                await UploadScreenshotAsync(url, ev, ct);

            var ok = await PostEventBatchAsync(url, events, ct);
            if (ok)
            {
                _store.MarkSent(events.Select(e => e.EventId.ToString()).ToList(), 1);
                sent += events.Count;
            }
            else
            {
                _store.MarkSent(events.Select(e => e.EventId.ToString()).ToList(), 2);
                failed += events.Count;
                _discovery.MarkUnreachable();
                break;
            }
        }

        PurgeOldData();
        HeartbeatWorker.LastSyncCompletedMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

        if (sent > 0 || failed > 0)
            _logger.LogInformation("Sync: sent={S} failed={F}", sent, failed);

        WriteEvent(nameof(EventType.SyncCompleted), sent, failed);
    }

    private async Task<bool> PostEventBatchAsync(string url, List<ActivityEvent> events, CancellationToken ct)
    {
        try
        {
            var now     = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var payload = events.Select(e => BuildEventDto(e)).ToList();
            var batch   = new { client_ts = now, events = payload };

            using var req = new HttpRequestMessage(HttpMethod.Post, $"{url}/api/v1/events");
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = JsonContent.Create(batch, options: _jsonOpts);

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            _logger.LogWarning("PostEvents failed: {Msg}", ex.Message);
            _errors.Report("http_sync_events", ex.Message);
            return false;
        }
    }

    private async Task UploadScreenshotAsync(string url, ActivityEvent ev, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(ev.ScreenshotPath)) return;

        try
        {
            // screenshot_path is relative: "{YYYYMMDD}\{event_id}.webp"
            var parts = ev.ScreenshotPath.Replace('\\', '/').Split('/');
            if (parts.Length < 2) return;

            var date    = parts[0];
            var eventId = Path.GetFileNameWithoutExtension(parts[1]);
            var local   = Path.Combine(_settings.ExpandedScreenshotDir, ev.ScreenshotPath);

            if (!File.Exists(local)) return;

            using var stream  = File.OpenRead(local);
            using var content = new StreamContent(stream);
            content.Headers.ContentType = new("image/webp");

            var endpoint = $"{url}/api/v1/screenshots/{_settings.MachineId}/{date}/{eventId}";
            using var req = new HttpRequestMessage(HttpMethod.Put, endpoint);
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = content;

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            resp.EnsureSuccessStatusCode();
        }
        catch (Exception ex)
        {
            _logger.LogDebug("Screenshot upload skipped for {Id}: {Msg}", ev.EventId, ex.Message);
        }
    }

    private static object BuildEventDto(ActivityEvent e)
    {
        return new
        {
            event_id         = e.EventId,
            session_id       = e.SessionId,
            machine_id       = e.MachineId,
            user_id          = e.UserId,
            timestamp_utc    = e.TimestampUtc,
            synced_ts        = e.SyncedTs,
            drift_ms         = e.DriftMs,
            drift_rate_ppm   = e.DriftRatePpm,
            sequence_idx     = e.SequenceIndex,
            layer            = e.Layer,
            event_type       = e.EventType,
            process_name     = e.ProcessName,
            app_version      = e.AppVersion,
            window_title     = e.WindowTitle,
            window_class     = e.WindowClass,
            element_type     = e.ElementType,
            element_name     = e.ElementName,
            element_auto_id  = e.ElementAutomationId,
            case_id          = e.CaseIdCandidate,
            screenshot_path  = e.ScreenshotPath,
            screenshot_dhash = e.ScreenshotDHash == 0 ? null : (long?)e.ScreenshotDHash,
            capture_reason   = e.CaptureReason,
            log_source       = e.LogSource,
            log_level        = e.LogLevel,
            raw_message      = e.RawMessage,
            message_hash     = e.MessageHash,
            document_path    = e.DocumentPath,
            document_name    = e.DocumentName,
            payload          = e,
        };
    }

    // ─── SQLite helpers moved to EventStore (shared connection + writeLock) ───

    private void PurgeOldData()
    {
        const int RetainDays = 7;
        var cutoffMs = DateTimeOffset.UtcNow.AddDays(-RetainDays).ToUnixTimeMilliseconds();

        try
        {
            var dbPath = _settings.ExpandedDbPath;
            using var conn = new SqliteConnection($"Data Source={dbPath};Pooling=False");
            conn.Open();
            var deleted = conn.Execute(
                "DELETE FROM events WHERE sent=1 AND timestamp_utc < @Cutoff",
                new { Cutoff = cutoffMs });
            if (deleted > 0)
                conn.Execute("PRAGMA wal_checkpoint(TRUNCATE)");
        }
        catch { }

        try
        {
            var root    = _settings.ExpandedScreenshotDir;
            var cutoff  = DateTime.UtcNow.AddDays(-RetainDays).ToString("yyyyMMdd");
            foreach (var dir in Directory.EnumerateDirectories(root))
                if (string.CompareOrdinal(Path.GetFileName(dir), cutoff) < 0)
                    Directory.Delete(dir, recursive: true);
        }
        catch { }
    }

    private void WriteEvent(string eventType, int sentCount, int failedCount)
    {
        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = raw,
            SyncedTs     = _ntp.SyncedTs(raw),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "agent",
            EventType    = eventType,
        });
    }
}
