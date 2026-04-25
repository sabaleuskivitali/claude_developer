using System.Collections.Concurrent;

namespace Seamlean.Agent.Management;

/// <summary>
/// Thread-safe registry of per-layer activity.
/// Updated by EventStore on every Insert; polled by LayerWatchdog every 2 minutes.
///
/// Thread safety:
///   - LayerState fields use Interlocked / volatile / MinuteWindow (internally locked).
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

    /// <summary>
    /// Circular sliding window: N slots of 1 minute each.
    /// Slot is reset when a new minute writes to the same index (modulo N).
    /// Read() always returns the correct count for the last N minutes — no stale buckets.
    /// </summary>
    private sealed class MinuteWindow(int size = 5)
    {
        private readonly long[] _minutes = new long[size];
        private readonly int[]  _counts  = new int[size];
        private readonly object _lock    = new();

        public void Record()
        {
            var minute = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 60_000;
            lock (_lock)
            {
                var idx = (int)(minute % size);
                if (_minutes[idx] != minute) { _minutes[idx] = minute; _counts[idx] = 0; }
                _counts[idx]++;
            }
        }

        public int Read()
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 60_000;
            lock (_lock)
            {
                int total = 0;
                for (int i = 0; i < size; i++)
                    if (now - _minutes[i] < size)
                        total += _counts[i];
                return total;
            }
        }
    }

    private sealed class LayerState
    {
        public long _lastEventMs;
        public volatile string _status = "inactive";
        public volatile bool   _isIdle;

        public readonly MinuteWindow EventWindow = new(5);
        public readonly MinuteWindow ErrorWindow = new(5);

        public LayerSnapshot Snapshot() => new(
            Interlocked.Read(ref _lastEventMs),
            EventWindow.Read(),
            ErrorWindow.Read(),
            _status, _isIdle);
    }

    public static readonly string[] KnownLayers =
        ["window", "visual", "system", "applogs", "browser", "agent"];

    // Silence threshold per layer (minutes). 0 = not monitored.
    public static readonly IReadOnlyDictionary<string, int> StuckThresholds =
        new Dictionary<string, int>
        {
            ["window"]  = 5,
            ["visual"]  = 15,
            ["system"]  = 10,
            ["applogs"] = 5,
            ["browser"] = 0,   // Chrome may not be open — never "stuck"
            ["agent"]   = 3,
        };

    private readonly ConcurrentDictionary<string, LayerState> _states;

    public LayerHealthTracker()
    {
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
        var state = _states.GetOrAdd(layer, _ => new LayerState());
        Interlocked.Exchange(ref state._lastEventMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        state.EventWindow.Record();
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
        state._isIdle = true;
        state._status = "idle";
    }

    public void RecordError(string layer, string? message = null)
    {
        var state = _states.GetOrAdd(layer, _ => new LayerState());
        state.ErrorWindow.Record();
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
