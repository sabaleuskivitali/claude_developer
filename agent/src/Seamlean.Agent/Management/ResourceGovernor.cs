using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Management;

/// <summary>
/// Tracks agent CPU and system RAM via EMA + dual-threshold hysteresis.
/// Write access through the concrete type (ResourceGovernorWorker).
/// Read access through IResourceGovernor (all capture layers).
/// </summary>
public sealed class ResourceGovernor : IResourceGovernor
{
    // EMA smoothing factor — ~23s time constant at 10s sample period
    private const double Alpha = 0.3;

    // Agent CPU thresholds (self-protection)
    private const double AgentCpuHigh = 5.0;
    private const double AgentCpuLow  = 3.0;

    // System RAM thresholds (courtesy throttle)
    private const double SystemRamHigh = 85.0;
    private const double SystemRamLow  = 75.0;

    private readonly EventStore     _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings  _settings;

    private double _smoothedAgentCpu;
    private double _smoothedSystemRam;
    private ThrottleLevel _level = ThrottleLevel.Normal;

    public ThrottleLevel Level => _level;

    public ResourceGovernor(EventStore store, NtpSynchronizer ntp, IOptions<AgentSettings> options)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
    }

    public void Update(double agentCpuPct, double systemRamPct)
    {
        _smoothedAgentCpu  = Alpha * agentCpuPct  + (1 - Alpha) * _smoothedAgentCpu;
        _smoothedSystemRam = Alpha * systemRamPct + (1 - Alpha) * _smoothedSystemRam;

        var newLevel = ComputeLevel();
        if (newLevel != _level)
            Transition(newLevel);
    }

    private ThrottleLevel ComputeLevel()
    {
        // Emergency wins over Courtesy
        if (_level == ThrottleLevel.Emergency)
        {
            if (_smoothedAgentCpu < AgentCpuLow)
                return _smoothedSystemRam > SystemRamHigh ? ThrottleLevel.Courtesy : ThrottleLevel.Normal;
            return ThrottleLevel.Emergency;
        }

        if (_smoothedAgentCpu > AgentCpuHigh)
            return ThrottleLevel.Emergency;

        if (_level == ThrottleLevel.Courtesy)
        {
            if (_smoothedSystemRam < SystemRamLow) return ThrottleLevel.Normal;
            return ThrottleLevel.Courtesy;
        }

        if (_smoothedSystemRam > SystemRamHigh)
            return ThrottleLevel.Courtesy;

        return ThrottleLevel.Normal;
    }

    private void Transition(ThrottleLevel newLevel)
    {
        var msg = $"ResourceGovernor: {_level} → {newLevel} " +
                  $"(agentCpu={_smoothedAgentCpu:F1}% systemRam={_smoothedSystemRam:F0}%)";
        _level = newLevel;

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
            EventType    = nameof(EventType.LayerError),
            RawMessage   = msg,
        });
    }
}
