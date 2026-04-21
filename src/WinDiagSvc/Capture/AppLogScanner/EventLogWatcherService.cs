using System.Diagnostics.Eventing.Reader;
using Microsoft.Extensions.Logging;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture.AppLogScanner;

/// <summary>
/// Discovers active Windows Event Log channels at startup and subscribes
/// to real-time events from each. One EventLogWatcher per channel.
/// </summary>
public sealed class EventLogWatcherService : IDisposable
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger _logger;

    private readonly List<EventLogWatcher> _watchers = new();

    public EventLogWatcherService(
        EventStore store,
        NtpSynchronizer ntp,
        AgentSettings settings,
        ILogger logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = settings;
        _logger   = logger;
    }

    public void Start()
    {
        var cutoff = DateTime.UtcNow.AddHours(-24);

        foreach (var logName in DiscoverActiveChannels(cutoff))
        {
            try
            {
                var query   = new EventLogQuery(logName, PathType.LogName, "*");
                var watcher = new EventLogWatcher(query);
                watcher.EventRecordWritten += (_, e) => HandleRecord(e.EventRecord, logName);
                watcher.Enabled = true;
                _watchers.Add(watcher);
            }
            catch (Exception ex)
            {
                _logger.LogDebug("Skip channel {Log}: {Msg}", logName, ex.Message);
            }
        }

        _logger.LogInformation("EventLogWatcherService: subscribed to {N} channels", _watchers.Count);
    }

    private static IEnumerable<string> DiscoverActiveChannels(DateTime cutoff)
    {
        var session = new EventLogSession();
        var cutoffStr = cutoff.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ");

        foreach (var name in session.GetLogNames())
        {
            if (string.IsNullOrEmpty(name)) continue;

            // CS1626: cannot yield inside a try-with-catch — use flag instead
            var hasEvents = false;
            try
            {
                var query = new EventLogQuery(
                    name, PathType.LogName,
                    $"*[System[TimeCreated[@SystemTime >= '{cutoffStr}']]]");
                using var reader = new EventLogReader(query);
                hasEvents = reader.ReadEvent() != null;
            }
            catch { /* channel inaccessible or no recent events */ }

            if (hasEvents) yield return name;
        }
    }

    private void HandleRecord(EventRecord? record, string logName)
    {
        if (record is null) return;
        try
        {
            var level   = record.Level.HasValue ? LevelName(record.Level.Value) : "Unknown";
            var message = SafeMessage(record);
            var hash    = EventStore.ComputeId(message ?? logName + record.Id);

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
                Layer        = "applogs",
                EventType    = nameof(EventType.EventLogEntry),
                LogSource    = $"EventLog:{logName}",
                LogLevel     = level,
                RawMessage   = message,
                MessageHash  = hash,
            });
        }
        catch { }
    }

    private static string? SafeMessage(EventRecord record)
    {
        try { return record.FormatDescription(); }
        catch { return record.Id.ToString(); }
    }

    private static string LevelName(byte level) => level switch
    {
        1 => "Error",
        2 => "Warning",
        3 => "Info",
        4 => "Info",
        5 => "Debug",
        _ => "Unknown",
    };

    public void Dispose()
    {
        foreach (var w in _watchers)
        {
            w.Enabled = false;
            w.Dispose();
        }
        _watchers.Clear();
    }
}
