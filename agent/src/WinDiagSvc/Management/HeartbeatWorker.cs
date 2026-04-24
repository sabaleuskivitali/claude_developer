using Microsoft.Extensions.Options;
using WinDiagSvc.Capture;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Management;

/// <summary>
/// Emits HeartbeatPulse every HeartbeatIntervalSeconds (default 60).
/// Payload includes NTP drift, pending event count, sync lag, and per-layer health stats.
/// Server uses drift fields as reference points for time interpolation.
/// </summary>
public sealed class HeartbeatWorker : BackgroundService
{
    private readonly EventStore          _store;
    private readonly NtpSynchronizer     _ntp;
    private readonly LayerHealthTracker  _tracker;
    private readonly AgentSettings       _settings;

    public static long LastSyncCompletedMs
    {
        get => Interlocked.Read(ref _lastSyncCompletedMs);
        set => Interlocked.Exchange(ref _lastSyncCompletedMs, value);
    }
    private static long _lastSyncCompletedMs;

    public HeartbeatWorker(
        EventStore store,
        NtpSynchronizer ntp,
        LayerHealthTracker tracker,
        IOptions<AgentSettings> options)
    {
        _store    = store;
        _ntp      = ntp;
        _tracker  = tracker;
        _settings = options.Value;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var interval = TimeSpan.FromSeconds(_settings.HeartbeatIntervalSeconds);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
            Pulse();
    }

    private void Pulse()
    {
        var nowMs      = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var syncLagSec = LastSyncCompletedMs > 0
            ? (int)((nowMs - LastSyncCompletedMs) / 1000)
            : -1;

        // Build per-layer stats for the payload
        var layerStats = new Dictionary<string, ActivityEvent.LayerStat>();
        foreach (var (layer, snap) in _tracker.GetSnapshot())
        {
            var secsSince = snap.LastEventMs > 0
                ? (int)((nowMs - snap.LastEventMs) / 1000)
                : int.MaxValue;
            layerStats[layer] = new ActivityEvent.LayerStat(
                LastEventSec: secsSince == int.MaxValue ? -1 : secsSince,
                Events5Min:   snap.Events5Min,
                Errors5Min:   snap.Errors5Min,
                Status:       snap.Status
            );
        }

        var version = typeof(HeartbeatWorker).Assembly.GetName().Version?.ToString(3) ?? "0.0.0";

        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = nowMs,
            SyncedTs     = _ntp.SyncedTs(nowMs),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "agent",
            EventType    = nameof(EventType.HeartbeatPulse),
            AgentVersion = version,
            Hostname     = Environment.MachineName,
            LayerStats   = layerStats,
        });
    }
}
