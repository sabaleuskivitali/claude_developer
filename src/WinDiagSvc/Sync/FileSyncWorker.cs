using Dapper;
using Microsoft.Data.Sqlite;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Capture;
using WinDiagSvc.Management;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Sync;

/// <summary>
/// Copies the previous day's SQLite file and screenshots to the SMB share.
/// Runs every SyncIntervalMinutes (default 60).
/// sent: 0=pending, 1=sent, 2=failed.
/// Current day is never synced — avoids file-lock conflicts.
/// </summary>
public sealed class FileSyncWorker : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<FileSyncWorker> _logger;

    public FileSyncWorker(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<FileSyncWorker> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // First sync after startup
        await Task.Delay(TimeSpan.FromMinutes(1), ct);
        await RunSyncAsync();

        var interval = TimeSpan.FromMinutes(_settings.SyncIntervalMinutes);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
            await RunSyncAsync();
    }

    private async Task RunSyncAsync()
    {
        var sentCount    = 0;
        var failedCount  = 0;

        try
        {
            var yesterday = DateTime.UtcNow.AddDays(-1).ToString("yyyyMMdd");
            var machineDir = Path.Combine(_settings.SharePath, _settings.MachineId);
            var targetDir  = Path.Combine(machineDir, yesterday);

            Directory.CreateDirectory(targetDir);

            // Copy SQLite snapshot (day file)
            var dbPath = _settings.ExpandedDbPath;
            if (File.Exists(dbPath))
            {
                var destDb = Path.Combine(targetDir, "events.db");
                await CopyWithRetryAsync(dbPath, destDb);
                sentCount++;
            }

            // Copy unsent screenshots from yesterday's cache dir
            var screenshotSrcDir  = Path.Combine(_settings.ExpandedScreenshotDir, yesterday);
            var screenshotDestDir = Path.Combine(targetDir, "screenshots");

            if (Directory.Exists(screenshotSrcDir))
            {
                Directory.CreateDirectory(screenshotDestDir);
                foreach (var file in Directory.EnumerateFiles(screenshotSrcDir, "*.webp"))
                {
                    var dest = Path.Combine(screenshotDestDir, Path.GetFileName(file));
                    if (File.Exists(dest)) continue;
                    await CopyWithRetryAsync(file, dest);
                    sentCount++;
                }
            }

            // Mark pending events as sent (all events with timestamp in yesterday)
            var yStart = new DateTimeOffset(
                DateTime.ParseExact(yesterday, "yyyyMMdd", null),
                TimeSpan.Zero).ToUnixTimeMilliseconds();
            var yEnd = yStart + 86_400_000L;

            MarkSent(yStart, yEnd, 1);

            _logger.LogInformation("Sync: sent={S}", sentCount);
        }
        catch (Exception ex)
        {
            failedCount++;
            _logger.LogWarning("Sync failed: {Msg}", ex.Message);

            // Mark all pending as failed so they appear in metrics
            MarkFailed();
        }

        HeartbeatWorker.LastSyncCompletedMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        WriteEvent(nameof(EventType.SyncCompleted), sentCount, failedCount);
    }

    private static async Task CopyWithRetryAsync(string src, string dest, int retries = 3)
    {
        for (var i = 0; i < retries; i++)
        {
            try
            {
                File.Copy(src, dest, overwrite: true);
                return;
            }
            catch when (i < retries - 1)
            {
                await Task.Delay(TimeSpan.FromSeconds(5));
            }
        }
    }

    private void MarkSent(long fromMs, long toMs, int status)
    {
        // Write directly to the store's internal connection is not exposed.
        // Use a separate connection for bulk status update (read-only concern).
        try
        {
            var dbPath = _settings.ExpandedDbPath;
            using var conn = new SqliteConnection($"Data Source={dbPath};Pooling=False");
            conn.Open();
            var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            conn.Execute(
                "UPDATE events SET sent=@S, sent_at=@At WHERE sent=0 AND timestamp_utc >= @From AND timestamp_utc < @To",
                new { S = status, At = now, From = fromMs, To = toMs });
        }
        catch { }
    }

    private void MarkFailed()
    {
        try
        {
            var dbPath = _settings.ExpandedDbPath;
            using var conn = new SqliteConnection($"Data Source={dbPath};Pooling=False");
            conn.Open();
            conn.Execute("UPDATE events SET sent=2 WHERE sent=0");
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
