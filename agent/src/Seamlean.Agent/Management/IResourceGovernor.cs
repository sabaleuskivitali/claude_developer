namespace Seamlean.Agent.Management;

public enum ThrottleLevel
{
    Normal,     // no restrictions
    Courtesy,   // system RAM high — stretch screenshot intervals
    Emergency,  // agent CPU anomaly — pause heavy layers
}

public interface IResourceGovernor
{
    ThrottleLevel Level { get; }
    bool IsThrottled => Level != ThrottleLevel.Normal;
}
