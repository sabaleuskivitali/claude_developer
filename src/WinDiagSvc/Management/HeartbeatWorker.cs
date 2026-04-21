using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Management;

/// <summary>
/// Emits HeartbeatPulse every HeartbeatIntervalSeconds (default 60).
/// Payload includes NTP drift, pending event count, sync lag.
/// Server uses these as drift reference points for time interpolation.
/// </summary>
public sealed class HeartbeatWorker : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;

    // Tracked by FileSyncWorker notification — simple volatile field is fine
    public static volatile long LastSyncCompletedMs;

    public HeartbeatWorker(
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
        var interval = TimeSpan.FromSeconds(_settings.HeartbeatIntervalSeconds);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
            Pulse();
    }

    private void Pulse()
    {
        var pending  = _store.CountPending();
        var failed   = _store.CountFailed();
        var nowMs    = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var syncLagSec = LastSyncCompletedMs > 0
            ? (int)((nowMs - LastSyncCompletedMs) / 1000)
            : -1;

        var raw = nowMs;
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
            EventType    = nameof(EventType.HeartbeatPulse),
            // Server reads drift fields directly from the event record columns.
            // Additional heartbeat fields are in the payload JSON via ToJson().
        });
    }
}
