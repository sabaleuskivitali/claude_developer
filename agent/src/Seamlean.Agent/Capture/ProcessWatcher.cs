using System.Management;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture;

/// <summary>
/// WMI-based process start/stop watcher. Simpler than ETW, no SeSystemProfilePrivilege required.
/// </summary>
public sealed class ProcessWatcher : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<ProcessWatcher> _logger;

    public ProcessWatcher(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<ProcessWatcher> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override Task ExecuteAsync(CancellationToken ct)
    {
        var thread = new Thread(() => WatchProcesses(ct));
        thread.IsBackground = true;
        thread.Start();
        return Task.Delay(Timeout.Infinite, ct);
    }

    private void WatchProcesses(CancellationToken ct)
    {
        ManagementEventWatcher? startWatcher = null;
        ManagementEventWatcher? stopWatcher  = null;

        try
        {
            startWatcher = new ManagementEventWatcher(
                new WqlEventQuery("SELECT * FROM Win32_ProcessStartTrace"));
            startWatcher.EventArrived += (_, e) =>
                HandleProcess(e.NewEvent, nameof(EventType.ProcessStart));
            startWatcher.Start();

            stopWatcher = new ManagementEventWatcher(
                new WqlEventQuery("SELECT * FROM Win32_ProcessStopTrace"));
            stopWatcher.EventArrived += (_, e) =>
                HandleProcess(e.NewEvent, nameof(EventType.ProcessStop));
            stopWatcher.Start();

            ct.WaitHandle.WaitOne();
        }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }
        finally
        {
            startWatcher?.Stop();
            startWatcher?.Dispose();
            stopWatcher?.Stop();
            stopWatcher?.Dispose();
        }
    }

    private void HandleProcess(ManagementBaseObject ev, string eventType)
    {
        try
        {
            var processName = ev["ProcessName"]?.ToString() ?? "";
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
                Layer        = "system",
                EventType    = eventType,
                ProcessName  = processName,
            });
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private void WriteLayerError(Exception ex) =>
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            SyncedTs     = _ntp.SyncedTs(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "system",
            EventType    = nameof(EventType.LayerError),
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });
}
