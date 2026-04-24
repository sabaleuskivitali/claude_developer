using System.Runtime.InteropServices;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture;

/// <summary>
/// Polls GetLastInputInfo every 5 seconds. Emits IdleStart / IdleEnd events
/// at configurable light and deep thresholds.
/// </summary>
public sealed class IdleDetector : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;

    private bool _idleActive;
    private bool _deepIdleActive;

    public IdleDetector(EventStore store, NtpSynchronizer ntp, IOptions<AgentSettings> options)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(5));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { CheckIdle(); }
            catch { /* non-critical */ }
        }
    }

    private void CheckIdle()
    {
        var info = new LASTINPUTINFO { cbSize = (uint)Marshal.SizeOf<LASTINPUTINFO>() };
        if (!GetLastInputInfo(ref info)) return;

        var idleMs = (long)(Environment.TickCount64 - info.dwTime);

        var lightThreshold = _settings.IdleLightThresholdMs;  // default 30 000
        var deepThreshold  = _settings.IdleDeepThresholdMs;   // default 120 000

        var isIdle      = idleMs >= lightThreshold;
        var isDeepIdle  = idleMs >= deepThreshold;

        if (isIdle && !_idleActive)
        {
            _idleActive = true;
            WriteEvent(nameof(EventType.IdleStart));
        }
        else if (!isIdle && _idleActive)
        {
            _idleActive      = false;
            _deepIdleActive  = false;
            WriteEvent(nameof(EventType.IdleEnd));
        }

        if (isDeepIdle && !_deepIdleActive)
        {
            _deepIdleActive = true;
            WriteEvent(nameof(EventType.IdleStart));   // second IdleStart marks deep threshold
        }
    }

    private void WriteEvent(string eventType)
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
            Layer        = "window",
            EventType    = eventType,
        });
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }

    [DllImport("user32.dll")]
    private static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
}
