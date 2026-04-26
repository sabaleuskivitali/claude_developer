using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Management;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture.AppLogScanner;

/// <summary>
/// Coordinator BackgroundService for Layer D.
/// Starts all four sub-scanners: EventLog, FileLog, RegistryMRU, LNK.
/// Each wrapped in try/catch — one failure does not stop the others.
/// </summary>
public sealed class AppLogScannerHost : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<AppLogScannerHost> _logger;
    private readonly LayerHealthTracker _tracker;

    public AppLogScannerHost(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<AppLogScannerHost> logger,
        LayerHealthTracker tracker)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
        _tracker  = tracker;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        StartSubScanner("EventLogWatcher", () =>
        {
            var svc = new EventLogWatcherService(_store, _ntp, _settings, _logger);
            svc.Start();
        });

        StartSubScanner("FileLogScanner", () =>
        {
            var svc = new FileLogScanner(_store, _ntp, _settings, _logger);
            svc.Start();
        });

        StartSubScanner("RegistryMruReader", () =>
        {
            var svc = new RegistryMruReader(_store, _ntp, _settings, _logger);
            svc.Start();
        });

        StartSubScanner("LnkWatcher", () =>
        {
            var svc = new LnkWatcher(_store, _ntp, _settings, _logger);
            svc.Start();
        });

        // Heartbeat: keep watchdog from false-alerting during idle/night periods.
        // Real events reset the clock too; this just guarantees a pulse every 2 minutes.
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(120));
        while (await timer.WaitForNextTickAsync(ct))
            _tracker.RecordEvent("applogs");
    }

    private void StartSubScanner(string name, Action start)
    {
        try
        {
            start();
            _logger.LogInformation("AppLogScanner: {Name} started", name);
        }
        catch (Exception ex)
        {
            _logger.LogWarning("AppLogScanner: {Name} failed to start — {Msg}", name, ex.Message);
            WriteLayerError(name, ex);
        }
    }

    private void WriteLayerError(string subLayer, Exception ex) =>
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            SyncedTs     = _ntp.SyncedTs(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "applogs",
            EventType    = nameof(EventType.LayerError),
            LogSource    = subLayer,
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });
}
