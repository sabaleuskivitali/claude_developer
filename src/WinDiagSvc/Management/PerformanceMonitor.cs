using System.Diagnostics;
using System.Text.Json;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Management;

/// <summary>
/// Collects performance metrics every PerformanceIntervalMinutes (default 5)
/// and writes a PerformanceSnapshot event. The payload JSON contains all
/// layer-level stats. Server reads it from the JSONB payload column.
/// </summary>
public sealed class PerformanceMonitor : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;

    // Layer error counters — incremented by each layer via static methods
    private static readonly Dictionary<string, LayerStat> _layerStats = new()
    {
        ["window"]  = new(),
        ["visual"]  = new(),
        ["system"]  = new(),
        ["applogs"] = new(),
        ["browser"] = new(),
        ["agent"]   = new(),
    };

    public static void RecordEvent(string layer)
    {
        if (_layerStats.TryGetValue(layer, out var s)) Interlocked.Increment(ref s.Events);
    }

    public static void RecordError(string layer)
    {
        if (_layerStats.TryGetValue(layer, out var s)) Interlocked.Increment(ref s.Errors);
    }

    public PerformanceMonitor(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var interval = TimeSpan.FromMinutes(_settings.PerformanceIntervalMinutes);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { TakeSnapshot(); }
            catch { /* non-critical */ }
        }
    }

    private void TakeSnapshot()
    {
        var proc         = Process.GetCurrentProcess();
        var cpuMs        = proc.TotalProcessorTime.TotalMilliseconds;
        var ramMb        = proc.WorkingSet64 / (1024.0 * 1024.0);
        var dbSizeMb     = _store.DbSizeBytes() / (1024.0 * 1024.0);
        var screenshotsMb = DirSizeMb(_settings.ExpandedScreenshotDir);
        var pending      = _store.CountPending();
        var failed       = _store.CountFailed();
        var version      = typeof(PerformanceMonitor).Assembly.GetName().Version?.ToString() ?? "1.0.0";

        // Snapshot and reset counters
        var layerSnapshot = new Dictionary<string, object>();
        foreach (var (name, stat) in _layerStats)
        {
            var events = Interlocked.Exchange(ref stat.Events, 0);
            var errors = Interlocked.Exchange(ref stat.Errors, 0);
            layerSnapshot[name] = new { events_5min = events, errors_5min = errors };
        }

        var nowMs     = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var syncLagMs = HeartbeatWorker.LastSyncCompletedMs > 0
            ? (int)((nowMs - HeartbeatWorker.LastSyncCompletedMs) / 60_000)
            : -1;

        var payload = new
        {
            agent_version          = version,
            process_cpu_ms         = (long)cpuMs,
            process_ram_mb         = Math.Round(ramMb, 1),
            sqlite_size_mb         = Math.Round(dbSizeMb, 2),
            screenshots_size_mb    = Math.Round(screenshotsMb, 1),
            events_pending         = pending,
            events_failed          = failed,
            smb_last_sync_ago_min  = syncLagMs,
            ntp_drift_ms           = _ntp.CurrentDriftMs,
            ntp_drift_rate_ppm     = Math.Round(_ntp.DriftRatePpm, 2),
            ntp_server_used        = _ntp.NtpServerUsed,
            ntp_last_rtt_ms        = _ntp.LastRoundTripMs,
            layer_stats            = layerSnapshot,
        };

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
            EventType    = nameof(EventType.PerformanceSnapshot),
        });
    }

    private static double DirSizeMb(string dir)
    {
        if (!Directory.Exists(dir)) return 0;
        try
        {
            return Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories)
                .Sum(f => new FileInfo(f).Length) / (1024.0 * 1024.0);
        }
        catch { return 0; }
    }

    private sealed class LayerStat
    {
        public int Events;
        public int Errors;
    }
}
