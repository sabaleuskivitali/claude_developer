using System.Security.Cryptography;
using System.Text;
using Dapper;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture.Meeting;
using Seamlean.Agent.Management;
using Seamlean.Agent.Models;

namespace Seamlean.Agent.Storage;

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
                screenshot_sent  INTEGER NOT NULL DEFAULT 0,
                payload          TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_unsent  ON events (sent, timestamp_utc);
            CREATE INDEX IF NOT EXISTS idx_session ON events (session_id, sequence_idx);
            CREATE INDEX IF NOT EXISTS idx_case    ON events (case_id) WHERE case_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_screenshots_unsent
                ON events (screenshot_sent, layer) WHERE layer = 'visual';
            """);

        // Migration for existing databases that don't have screenshot_sent yet
        try { _conn.Execute("ALTER TABLE events ADD COLUMN screenshot_sent INTEGER NOT NULL DEFAULT 0"); }
        catch { /* column already exists */ }

        _conn.Execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                meeting_id    TEXT    PRIMARY KEY,
                machine_id    TEXT    NOT NULL,
                user_id       TEXT    NOT NULL,
                started_at    INTEGER NOT NULL,
                ended_at      INTEGER,
                process_name  TEXT,
                window_title  TEXT,
                trigger       TEXT,
                mic_path      TEXT,
                loopback_path TEXT,
                mic_sent      INTEGER NOT NULL DEFAULT 0,
                loopback_sent INTEGER NOT NULL DEFAULT 0,
                meta_sent     INTEGER NOT NULL DEFAULT 0
            );
            """);
    }

    public void Insert(ActivityEvent ev)
    {
        // Update per-layer health tracker on every event write
        if (ev.EventType == nameof(EventType.IdleStart))
            _tracker?.MarkIdle(ev.Layer);
        else
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
                    ev.ScreenshotDHash,
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

    public List<ActivityEvent> ReadPending(int limit)
    {
        lock (_writeLock)
            return _conn.Query<ActivityEvent>(
                "SELECT * FROM events WHERE sent IN (0,2) ORDER BY timestamp_utc LIMIT @Limit",
                new { Limit = limit }).ToList();
    }

    public void MarkSent(List<string> eventIds, int status)
    {
        if (eventIds.Count == 0) return;
        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var ids  = string.Join(",", eventIds.Select(id => $"'{id}'"));
        lock (_writeLock)
            _conn.Execute(
                $"UPDATE events SET sent=@S, sent_at=@At WHERE LOWER(event_id) IN ({ids.ToLower()})",
                new { S = status, At = now });
    }

    public List<ScreenshotPending> GetUnsentScreenshots(int limit)
    {
        lock (_writeLock)
            return _conn.Query<ScreenshotPending>(
                """
                SELECT event_id AS EventId, screenshot_path AS ScreenshotPath FROM events
                WHERE layer = 'visual' AND screenshot_path IS NOT NULL AND screenshot_sent = 0
                ORDER BY timestamp_utc
                LIMIT @Limit
                """,
                new { Limit = limit }).ToList();
    }

    public void MarkScreenshotSent(string eventId, int status)
    {
        lock (_writeLock)
            _conn.Execute(
                "UPDATE events SET screenshot_sent = @S WHERE event_id = @Id",
                new { S = status, Id = eventId });
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

    // ─── Meetings ────────────────────────────────────────────────────────────

    public void InsertMeeting(MeetingRecord m)
    {
        lock (_writeLock)
            _conn.Execute("""
                INSERT OR IGNORE INTO meetings
                    (meeting_id, machine_id, user_id, started_at, process_name, window_title, trigger)
                VALUES
                    (@MeetingId, @MachineId, @UserId, @StartedAt, @ProcessName, @WindowTitle, @Trigger)
                """, m);
    }

    public void UpdateMeetingEnded(string meetingId, long endedAt, string? micPath, string? loopbackPath)
    {
        lock (_writeLock)
            _conn.Execute("""
                UPDATE meetings
                   SET ended_at = @EndedAt, mic_path = @MicPath, loopback_path = @LoopbackPath
                 WHERE meeting_id = @MeetingId
                """, new { MeetingId = meetingId, EndedAt = endedAt, MicPath = micPath, LoopbackPath = loopbackPath });
    }

    public List<MeetingRecord> GetPendingMeetings(int limit)
    {
        lock (_writeLock)
            return _conn.Query<MeetingRecord>(
                """
                SELECT * FROM meetings
                 WHERE ended_at IS NOT NULL
                   AND (meta_sent IN (0,2) OR mic_sent IN (0,2) OR loopback_sent IN (0,2))
                 ORDER BY started_at
                 LIMIT @Limit
                """, new { Limit = limit }).ToList();
    }

    public void SetMeetingMicSent(string meetingId, int status)
    {
        lock (_writeLock)
            _conn.Execute("UPDATE meetings SET mic_sent=@S WHERE meeting_id=@Id",
                new { S = status, Id = meetingId });
    }

    public void SetMeetingLoopbackSent(string meetingId, int status)
    {
        lock (_writeLock)
            _conn.Execute("UPDATE meetings SET loopback_sent=@S WHERE meeting_id=@Id",
                new { S = status, Id = meetingId });
    }

    public void SetMeetingMetaSent(string meetingId, int status)
    {
        lock (_writeLock)
            _conn.Execute("UPDATE meetings SET meta_sent=@S WHERE meeting_id=@Id",
                new { S = status, Id = meetingId });
    }

    // ─────────────────────────────────────────────────────────────────────────

    public static string ComputeId(string input)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(input))).ToLower();

    public void Dispose()
    {
        _conn.Close();
        _conn.Dispose();
    }
}

public sealed record ScreenshotPending(string EventId, string ScreenshotPath);
