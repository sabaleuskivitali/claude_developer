using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Session;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture;

/// <summary>
/// ETW Kernel-File provider for FileCreate / FileWrite events.
/// Filtered to configured extensions (office docs, 1C, etc.).
/// Requires LocalSystem (SeSystemProfilePrivilege).
/// </summary>
public sealed class FileEventCapture : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<FileEventCapture> _logger;

    private static readonly HashSet<string> _fileOps = new(StringComparer.OrdinalIgnoreCase)
        { "FileCreate", "FileWrite" };

    public FileEventCapture(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<FileEventCapture> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override Task ExecuteAsync(CancellationToken ct)
    {
        var thread = new Thread(() => WatchFiles(ct));
        thread.IsBackground = true;
        thread.Start();
        return Task.Delay(Timeout.Infinite, ct);
    }

    private void WatchFiles(CancellationToken ct)
    {
        TraceEventSession? session = null;
        try
        {
            var extensions = new HashSet<string>(
                _settings.FileExtensionsToTrack,
                StringComparer.OrdinalIgnoreCase);

            session = new TraceEventSession("WinDiagFileSession");
            session.EnableKernelProvider(KernelTraceEventParser.Keywords.FileIO |
                                         KernelTraceEventParser.Keywords.FileIOInit);

            session.Source.Kernel.FileIOCreate += e =>
            {
                var ext = Path.GetExtension(e.FileName);
                if (!extensions.Contains(ext)) return;
                Store(e.FileName, nameof(EventType.FileCreate));
            };

            session.Source.Kernel.FileIOWrite += e =>
            {
                var ext = Path.GetExtension(e.FileName);
                if (!extensions.Contains(ext)) return;
                Store(e.FileName, nameof(EventType.FileWrite));
            };

            ct.Register(() => session.Stop());
            session.Source.Process();
        }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }
        finally
        {
            session?.Dispose();
        }
    }

    private void Store(string filePath, string eventType)
    {
        try
        {
            var ext  = Path.GetExtension(filePath);
            var name = Path.GetFileName(filePath);
            var raw  = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

            _store.Insert(new ActivityEvent
            {
                SessionId     = _store.SessionId,
                MachineId     = _settings.MachineId,
                UserId        = _settings.UserId,
                TimestampUtc  = raw,
                SyncedTs      = _ntp.SyncedTs(raw),
                DriftMs       = _ntp.CurrentDriftMs,
                DriftRatePpm  = _ntp.DriftRatePpm,
                Layer         = "system",
                EventType     = eventType,
                DocumentPath  = filePath.Length > 500 ? filePath[..500] : filePath,
                DocumentName  = name,
                FileExtension = ext,
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
