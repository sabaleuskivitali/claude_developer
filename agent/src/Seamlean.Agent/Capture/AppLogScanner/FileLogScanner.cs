using System.IO;
using Microsoft.Extensions.Logging;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture.AppLogScanner;

/// <summary>
/// Discovers application log files under %AppData%, %LocalAppData%, %ProgramData%.
/// Subscribes with FileSystemWatcher, reads deltas on change.
/// Re-discovery runs once per day + at startup.
/// </summary>
public sealed class FileLogScanner : IDisposable
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger _logger;

    private readonly Dictionary<string, long> _offsets   = new();
    private readonly List<FileSystemWatcher>   _watchers  = new();
    private Timer? _rediscoveryTimer;

    private static readonly string[] _scanRoots =
    [
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
    ];

    private static readonly string[] _logPatterns =
        ["*.log", "*.txt"];

    private static readonly string[] _logDirKeywords =
        ["log", "logs", "Log", "Logs"];

    public FileLogScanner(
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
        Discover();
        _rediscoveryTimer = new Timer(_ => Discover(), null,
            TimeSpan.FromHours(24), TimeSpan.FromHours(24));
    }

    private void Discover()
    {
        var cutoff = DateTime.UtcNow.AddDays(-7);
        var found  = 0;

        foreach (var root in _scanRoots)
        {
            if (!Directory.Exists(root)) continue;
            try
            {
                foreach (var dir in EnumerateLogDirs(root, depth: 3))
                {
                    foreach (var pattern in _logPatterns)
                    {
                        foreach (var file in Directory.EnumerateFiles(dir, pattern))
                        {
                            try
                            {
                                var info = new FileInfo(file);
                                if (info.Length > 100 * 1024 * 1024) continue; // >100 MB skip
                                if (info.LastWriteTimeUtc < cutoff)   continue;
                                if (_offsets.ContainsKey(file))       continue;

                                // Seed offset to end — don't re-read historical content
                                _offsets[file] = info.Length;
                                WatchFile(file);
                                found++;
                            }
                            catch { }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogDebug("FileLogScanner discovery error in {Root}: {Msg}", root, ex.Message);
            }
        }

        if (found > 0)
            _logger.LogInformation("FileLogScanner: discovered {N} new log files", found);
    }

    private static IEnumerable<string> EnumerateLogDirs(string root, int depth)
    {
        if (depth == 0) yield break;
        IEnumerable<string> dirs;
        try { dirs = Directory.EnumerateDirectories(root); }
        catch { yield break; }

        foreach (var dir in dirs)
        {
            var name = Path.GetFileName(dir);
            if (_logDirKeywords.Any(k => name.Contains(k, StringComparison.OrdinalIgnoreCase)))
                yield return dir;

            foreach (var sub in EnumerateLogDirs(dir, depth - 1))
                yield return sub;
        }
    }

    private void WatchFile(string filePath)
    {
        var dir = Path.GetDirectoryName(filePath)!;
        var existing = _watchers.FirstOrDefault(w => w.Path == dir);
        if (existing != null) return; // already watching this dir

        try
        {
            var w = new FileSystemWatcher(dir)
            {
                Filter                = "*",
                NotifyFilter          = NotifyFilters.LastWrite | NotifyFilters.Size,
                EnableRaisingEvents   = true,
                IncludeSubdirectories = false,
            };
            w.Changed += (_, e) => ReadDelta(e.FullPath);
            _watchers.Add(w);
        }
        catch { }
    }

    private void ReadDelta(string filePath)
    {
        if (!_offsets.TryGetValue(filePath, out var offset)) return;

        try
        {
            using var fs = new FileStream(filePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            if (fs.Length <= offset) return;

            fs.Seek(offset, SeekOrigin.Begin);
            using var reader = new StreamReader(fs);
            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                offset = fs.Position;
                _offsets[filePath] = offset;

                if (string.IsNullOrWhiteSpace(line)) continue;

                var level = DetectLevel(line);
                var hash  = EventStore.ComputeId(line);

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
                    EventType    = nameof(EventType.FileLogEntry),
                    LogSource    = $"File:{filePath}",
                    LogLevel     = level,
                    RawMessage   = line,
                    MessageHash  = hash,
                });
            }
        }
        catch { }
    }

    private static string DetectLevel(string line)
    {
        var upper = line.ToUpperInvariant();
        if (upper.Contains("ERROR") || upper.Contains("EXCEPTION") || upper.Contains("FATAL")) return "Error";
        if (upper.Contains("WARN"))  return "Warning";
        if (upper.Contains("DEBUG")) return "Debug";
        if (upper.Contains("INFO"))  return "Info";
        return "Unknown";
    }

    public void Dispose()
    {
        _rediscoveryTimer?.Dispose();
        foreach (var w in _watchers) w.Dispose();
        _watchers.Clear();
    }
}
