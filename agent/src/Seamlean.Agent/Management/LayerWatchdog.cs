using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Management;

/// <summary>
/// Polls LayerHealthTracker every WatchdogIntervalMinutes (default 2).
/// Detects stuck layers, emits LayerStuck events, restarts whole agent when critical.
///
/// Since we're a Scheduled Task (not a Windows Service), individual layer restart
/// is not feasible without major refactoring. Instead:
///   - 1st detection: emit LayerStuck, mark in tracker
///   - 2nd consecutive detection: Environment.Exit(1) → Scheduled Task restarts agent
///     (configured with 3 retries, 1-minute interval in the installer)
/// </summary>
public sealed class LayerWatchdog : BackgroundService
{
    private readonly LayerHealthTracker _tracker;
    private readonly EventStore         _store;
    private readonly NtpSynchronizer    _ntp;
    private readonly AgentSettings      _settings;
    private readonly ILogger<LayerWatchdog> _logger;

    // Tracks consecutive stuck cycles per layer before triggering restart.
    // ConcurrentDictionary: CheckLayers runs on timer thread, Restart* on command-poller thread.
    private readonly ConcurrentDictionary<string, int> _stuckCycles = new();

    // How many consecutive stuck detections before forcing restart
    private const int RestartAfterCycles = 2;

    public LayerWatchdog(
        LayerHealthTracker tracker,
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<LayerWatchdog> logger)
    {
        _tracker  = tracker;
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Wait a bit after startup so layers have time to emit their first events
        await Task.Delay(TimeSpan.FromMinutes(3), ct);

        var interval = TimeSpan.FromMinutes(_settings.WatchdogIntervalMinutes);
        using var timer = new PeriodicTimer(interval);

        while (await timer.WaitForNextTickAsync(ct))
            CheckLayers();
    }

    private void CheckLayers()
    {
        var stuckLayers = new List<string>();

        foreach (var (layer, threshold) in LayerHealthTracker.StuckThresholds)
        {
            if (threshold == 0) continue;   // browser: not monitored

            var snapshot  = _tracker.GetSnapshot().GetValueOrDefault(layer);
            if (snapshot.IsIdle) continue;  // machine idle — not stuck

            var secsSince = _tracker.SecondsSinceLastEvent(layer);
            var threshSec = threshold * 60;

            if (secsSince > threshSec)
            {
                _logger.LogWarning("LayerWatchdog: {Layer} stuck — {Secs}s since last event (threshold {Thresh}s)",
                    layer, secsSince, threshSec);

                _tracker.MarkStuck(layer);
                _stuckCycles.AddOrUpdate(layer, 1, (_, c) => c + 1);
                stuckLayers.Add(layer);

                EmitLayerStuck(layer, secsSince, _stuckCycles[layer]);
            }
            else
            {
                _stuckCycles[layer] = 0;
            }
        }

        // If any critical layer has been stuck for 2+ consecutive check cycles → restart
        var criticalLayers = new[] { "visual", "window", "agent" };
        var needsRestart = stuckLayers
            .Where(l => criticalLayers.Contains(l))
            .Any(l => _stuckCycles.TryGetValue(l, out var c) && c >= RestartAfterCycles);

        if (needsRestart)
        {
            var trigger = stuckLayers
                .Where(l => criticalLayers.Contains(l) && _stuckCycles.GetValueOrDefault(l) >= RestartAfterCycles)
                .First();

            _logger.LogError("LayerWatchdog: critical layer '{Layer}' stuck for {Cycles} cycles — restarting agent",
                trigger, _stuckCycles[trigger]);

            EmitLayerRestarted(trigger);

            // Small delay to ensure event is written before exit
            Thread.Sleep(500);
            Environment.Exit(1);  // Scheduled Task restarts automatically
        }
    }

    // ── Command entry point ────────────────────────────────────────────────────

    /// <summary>
    /// Initiates an agent restart triggered by a remote command.
    /// </summary>
    public void RestartAgent(string reason)
    {
        _logger.LogWarning("LayerWatchdog: restart requested — {Reason}", reason);
        EmitLayerRestarted(reason);
        Thread.Sleep(300);
        Environment.Exit(1);
    }

    /// <summary>
    /// Initiates a restart for a specific layer (restarts entire process, logs trigger).
    /// </summary>
    public void RestartLayer(string layer)
    {
        _logger.LogWarning("LayerWatchdog: restarting layer '{Layer}' (full process restart)", layer);
        EmitLayerRestarted(layer);
        Thread.Sleep(300);
        Environment.Exit(1);
    }

    // ── Event helpers ──────────────────────────────────────────────────────────

    private void EmitLayerStuck(string layer, int secsSince, int cycleCount)
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
            EventType    = nameof(EventType.LayerStuck),
            RawMessage   = $"layer={layer} silent_sec={secsSince} cycle={cycleCount}",
        });
    }

    private void EmitLayerRestarted(string trigger)
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
            EventType    = nameof(EventType.LayerRestarted),
            RawMessage   = $"trigger={trigger}",
        });
    }
}
