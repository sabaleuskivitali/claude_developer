using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture;

/// <summary>
/// Watches common user document directories for file create/write events.
/// Uses FileSystemWatcher — simpler than ETW, no privilege requirements.
/// Filtered to configured extensions (office docs, 1C, etc.).
/// ETW kernel-level tracking can replace this post-pilot if deeper coverage is needed.
/// </summary>
public sealed class FileEventCapture : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<FileEventCapture> _logger;

    private readonly List<FileSystemWatcher> _watchers = new();

    // Directories to watch (environment-expanded at start)
    private static readonly string[] _watchRoots =
    [
        Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
        Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
    ];

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

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var extensions = new HashSet<string>(
            _settings.FileExtensionsToTrack,
            StringComparer.OrdinalIgnoreCase);

        foreach (var root in _watchRoots)
        {
            if (!Directory.Exists(root)) continue;
            try
            {
                var w = new FileSystemWatcher(root)
                {
                    IncludeSubdirectories = true,
                    NotifyFilter          = NotifyFilters.FileName | NotifyFilters.LastWrite,
                    EnableRaisingEvents   = true,
                };
                w.Created += (_, e) => Handle(e.FullPath, nameof(EventType.FileCreate), extensions);
                w.Changed += (_, e) => Handle(e.FullPath, nameof(EventType.FileWrite),  extensions);
                _watchers.Add(w);
                _logger.LogDebug("FileEventCapture watching: {Dir}", root);
            }
            catch (Exception ex)
            {
                _logger.LogWarning("FileEventCapture: failed to watch {Dir}: {Msg}", root, ex.Message);
            }
        }

        _logger.LogInformation("FileEventCapture: watching {N} directories", _watchers.Count);

        await Task.Delay(Timeout.Infinite, ct);
    }

    private void Handle(string fullPath, string eventType, HashSet<string> extensions)
    {
        try
        {
            var ext = Path.GetExtension(fullPath);
            if (!extensions.Contains(ext)) return;

            var name = Path.GetFileName(fullPath);
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
                DocumentPath  = fullPath.Length > 500 ? fullPath[..500] : fullPath,
                DocumentName  = name,
                FileExtension = ext,
            });
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    public override Task StopAsync(CancellationToken ct)
    {
        foreach (var w in _watchers) w.Dispose();
        _watchers.Clear();
        return base.StopAsync(ct);
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
