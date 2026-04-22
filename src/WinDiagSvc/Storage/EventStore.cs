using System.Security.Cryptography;
using System.Text;
using Dapper;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Options;
using WinDiagSvc.Management;
using WinDiagSvc.Models;

namespace WinDiagSvc.Storage;

/// <summary>
/// Single SQLite connection, WAL mode, synchronous writes.
/// All capture layers share this instance via DI (singleton).
/// Thread safety: lock per write operation.
/// </summary>
public sealed class EventStore : IDisposable
{
    private readonly SqliteConnection _conn;
    private readonly AgentSettings    _settings;
    private readonly LayerHealthTracker? _tracker;
    private readonly object _writeLock = new();
    private int _sequenceIndex;

    public Guid SessionId { get; } = Guid.NewGuid();

    public EventStore(IOptions<AgentSettings> options, LayerHealthTracker tracker)
    {
        _settings = options.Value;
        _tracker  = tracker;

        var dbPath = _settings.ExpandedDbPath;
        Directory.CreateDirectory(Path.GetDirectoryName(dbPath)!);

        _conn = new SqliteConnection($"Data Source={dbPath};Pooling=False");
        _conn.Open();

        ApplyPragmas();
        EnsureSchema();
    }

    private void ApplyPragmas()
    {
        _conn.Execute("PRAGMA journal_mode = WAL");
        _conn.Execute("PRAGMA synchronous  = NORMAL");
        _conn.Execute("PRAGMA cache_size   = -4096");  // 4 MB cache
        _conn.Execute("PRAGMA temp_store   = MEMORY");
    }

    private void EnsureSchema()
    {
        _conn.Execute("""
            CREATE TABLE IF NOT EXISTS events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT    NOT NULL UNIQUE,
                session_id       TEXT    NOT NULL,
                machine_id       TEXT    NOT NULL,
                user_id          TEXT    NOT NULL,
                timestamp_utc    INTEGER NOT NULL,
                synced_ts        INTEGER NOT NULL,
                drift_ms         INTEGER NOT NULL DEFAULT 0,
                drift_rate_ppm   REAL    NOT NULL DEFAULT 0,
                sequence_idx     INTEGER NOT NULL,
                layer            TEXT    NOT NULL,
                event_type       TEXT    NOT NULL,
                process_name     TEXT,
                app_version      TEXT,
                window_title     TEXT,
                window_class     TEXT,
                element_type     TEXT,
                element_name     TEXT,
                element_auto_id  TEXT,
                case_id          TEXT,
                screenshot_path  TEXT,
                screenshot_dhash INTEGER,
                capture_reason   TEXT,
                log_source       TEXT,
                log_level        TEXT,
                raw_message      TEXT,
                message_hash     TEXT,
                document_path    TEXT,
                document_name    TEXT,
                sent             INTEGER NOT NULL DEFAULT 0,
                sent_at          INTEGER,
                payload          TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_unsent  ON events (sent, timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_session ON events (session_id, sequence_idx);
            CREATE INDEX IF NOT EXISTS idx_case    ON events (case_id) WHERE case_id IS NOT NULL;
            """);
    }

    public void Insert(ActivityEvent ev)
    {
        // Update per-layer health tracker on every event write
        _tracker?.RecordEvent(ev.Layer);
        if (ev.EventType == nameof(EventType.LayerError))
            _tracker?.RecordError(ev.Layer, ev.RawMessage);

        var idx = Interlocked.Increment(ref _sequenceIndex);
        var json = ev.ToJson();

        lock (_writeLock)
        {
            _conn.Execute("""
                INSERT OR IGNORE INTO events (
                    event_id, session_id, machine_id, user_id,
                    timestamp_utc, synced_ts, drift_ms, drift_rate_ppm,
                    sequence_idx, layer, event_type,
                    process_name, app_version, window_title, window_class,
                    element_type, element_name, element_auto_id,
                    case_id, screenshot_path, screenshot_dhash, capture_reason,
                    log_source, log_level, raw_message, message_hash,
                    document_path, document_name,
                    sent, payload
                ) VALUES (
                    @EventId, @SessionId, @MachineId, @UserId,
                    @TimestampUtc, @SyncedTs, @DriftMs, @DriftRatePpm,
                    @SequenceIdx, @Layer, @EventType,
                    @ProcessName, @AppVersion, @WindowTitle, @WindowClass,
                    @ElementType, @ElementName, @ElementAutomationId,
                    @CaseId, @ScreenshotPath, @ScreenshotDHash, @CaptureReason,
                    @LogSource, @LogLevel, @RawMessage, @MessageHash,
                    @DocumentPath, @DocumentName,
                    0, @Payload
                )
                """,
                new
                {
                    ev.EventId,
                    ev.SessionId,
                    ev.MachineId,
                    ev.UserId,
                    ev.TimestampUtc,
                    ev.SyncedTs,
                    ev.DriftMs,
                    ev.DriftRatePpm,
                    SequenceIdx = idx,
                    ev.Layer,
                    ev.EventType,
                    ev.ProcessName,
                    ev.AppVersion,
                    ev.WindowTitle,
                    ev.WindowClass,
                    ev.ElementType,
                    ev.ElementName,
                    ElementAutomationId = ev.ElementAutomationId,
                    CaseId = ev.CaseId,
                    ev.ScreenshotPath,
                    ScreenshotDHash = (long)ev.ScreenshotDHash,
                    ev.CaptureReason,
                    ev.LogSource,
                    ev.LogLevel,
                    RawMessage = SanitizeMessage(ev.RawMessage),
                    ev.MessageHash,
                    ev.DocumentPath,
                    ev.DocumentName,
                    Payload = json,
                });
        }
    }

    public int CountPending()
    {
        lock (_writeLock)
            return _conn.ExecuteScalar<int>("SELECT COUNT(*) FROM events WHERE sent = 0");
    }

    public int CountFailed()
    {
        lock (_writeLock)
            return _conn.ExecuteScalar<int>("SELECT COUNT(*) FROM events WHERE sent = 2");
    }

    public long DbSizeBytes()
    {
        var path = _settings.ExpandedDbPath;
        return File.Exists(path) ? new FileInfo(path).Length : 0;
    }

    // Strip credential keywords from log messages (privacy)
    private static readonly string[] _sensitiveKeywords =
        ["password", "passwd", "secret", "token", "credential", "пароль", "auth"];

    private static string? SanitizeMessage(string? msg)
    {
        if (msg is null) return null;
        foreach (var kw in _sensitiveKeywords)
            if (msg.Contains(kw, StringComparison.OrdinalIgnoreCase))
                return null;
        return msg.Length > 500 ? msg[..500] : msg;
    }

    public static string ComputeId(string input)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(input))).ToLower();

    public void Dispose()
    {
        _conn.Close();
        _conn.Dispose();
    }
}
