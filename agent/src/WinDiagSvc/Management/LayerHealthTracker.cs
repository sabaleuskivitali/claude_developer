using System.Collections.Concurrent;

namespace WinDiagSvc.Management;

/// <summary>
/// Thread-safe registry of per-layer activity.
/// Updated by EventStore on every Insert; polled by LayerWatchdog every 2 minutes.
///
/// Thread safety:
///   - Bucket dictionaries use ConcurrentDictionary.
///   - Scalar fields (LastEventMs, Events5Min, Errors5Min) use Interlocked / volatile.
///   - GetSnapshot() returns a value-copy struct so readers never race with writers.
/// </summary>
public sealed class LayerHealthTracker
{
    /// <summary>Immutable value snapshot — safe to read from any thread.</summary>
    public readonly record struct LayerSnapshot(
        long   LastEventMs,
        int    Events5Min,
        int    Errors5Min,
        string Status,
        bool   IsIdle);

    // Mutable live state, only ever accessed through interlocked / volatile helpers
    private sealed class LayerState
    {
        public long _lastEventMs;                // Interlocked.Read/Exchange
        public volatile int _events5Min;
        public volatile int _errors5Min;
        public volatile string _status = "inactive";  // ok | stuck | inactive | error | idle
        public volatile bool _isIdle;

        public readonly ConcurrentDictionary<long, int> EventBuckets = new();
        public readonly ConcurrentDictionary<long, int> ErrorBuckets = new();

        public LayerSnapshot Snapshot() => new(
            Interlocked.Read(ref _lastEventMs),
            _events5Min, _errors5Min, _status, _isIdle);
    }

    public static readonly string[] KnownLayers =
        ["window", "visual", "system", "applogs", "browser", "agent"];

    // Silence threshold per layer (minutes). 0 = not monitored.
    public static readonly IReadOnlyDictionary<string, int> StuckThresholds =
        new Dictionary<string, int>
        {
            ["window"]  = 5,
            ["visual"]  = 3,
            ["system"]  = 10,
            ["applogs"] = 5,
            ["browser"] = 0,   // Chrome may not be open — never "stuck"
            ["agent"]   = 3,
        };

    private readonly ConcurrentDictionary<string, LayerState> _states;

    public LayerHealthTracker()
    {
        // Pre-seed all known layers with current time so the watchdog doesn't
        // trigger immediately on startup if a layer hasn't fired its first event yet.
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _states = new ConcurrentDictionary<string, LayerState>(
            KnownLayers.Select(l =>
            {
                var s = new LayerState();
                Interlocked.Exchange(ref s._lastEventMs, nowMs);
                return KeyValuePair.Create(l, s);
            })
        );
    }

    public void RecordEvent(string layer)
    {
        var state  = _states.GetOrAdd(layer, _ => new LayerState());
        var nowMs  = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var bucket = nowMs / 60_000;

        Interlocked.Exchange(ref state._lastEventMs, nowMs);
        state.EventBuckets.AddOrUpdate(bucket, 1, (_, v) => v + 1);

        var cutoff = bucket - 5;
        foreach (var k in state.EventBuckets.Keys)
            if (k < cutoff) state.EventBuckets.TryRemove(k, out _);

        state._events5Min = state.EventBuckets.Values.Sum();
        state._isIdle = false;

        if (state._status is "stuck" or "inactive" or "idle")
            state._status = "ok";
    }

    /// <summary>
    /// Called when an IdleStart event is written for this layer.
    /// Resets the silence timer so the watchdog doesn't false-positive during user idle.
    /// </summary>
    public void MarkIdle(string layer)
    {
        var state = _states.GetOrAdd(layer, _ => new LayerState());
        Interlocked.Exchange(ref state._lastEventMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        state._isIdle  = true;
        state._status  = "idle";
    }

    public void RecordError(string layer, string? message = null)
    {
        var state  = _states.GetOrAdd(layer, _ => new LayerState());
        var bucket = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 60_000;

        state.ErrorBuckets.AddOrUpdate(bucket, 1, (_, v) => v + 1);

        var cutoff = bucket - 5;
        foreach (var k in state.ErrorBuckets.Keys)
            if (k < cutoff) state.ErrorBuckets.TryRemove(k, out _);

        state._errors5Min = state.ErrorBuckets.Values.Sum();
        state._status = "error";
    }

    public void MarkStuck(string layer) =>
        _states.GetOrAdd(layer, _ => new LayerState())._status = "stuck";

    /// <summary>
    /// Returns an immutable snapshot dict — safe to iterate without locks.
    /// </summary>
    public IReadOnlyDictionary<string, LayerSnapshot> GetSnapshot()
    {
        var result = new Dictionary<string, LayerSnapshot>(KnownLayers.Length);
        foreach (var layer in KnownLayers)
            result[layer] = _states.TryGetValue(layer, out var s)
                ? s.Snapshot()
                : new LayerSnapshot(0, 0, 0, "inactive", false);
        return result;
    }

    public int SecondsSinceLastEvent(string layer)
    {
        if (!_states.TryGetValue(layer, out var s)) return int.MaxValue;
        var last = Interlocked.Read(ref s._lastEventMs);
        if (last == 0) return int.MaxValue;
        return (int)((DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - last) / 1000);
    }
}
