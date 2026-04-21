using Microsoft.Extensions.Logging;
using Microsoft.Win32;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture.AppLogScanner;

/// <summary>
/// Polls Registry MRU keys every 30 seconds.
/// Emits RecentDocumentOpened events when a new entry appears.
/// DocumentName = ready case_id candidate.
/// </summary>
public sealed class RegistryMruReader : IDisposable
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger _logger;

    private readonly HashSet<string> _seen = new();
    private Timer? _timer;

    private static readonly string[] _mruRoots =
    [
        @"Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs",
        @"Software\Microsoft\Office",   // sub-enumerated for all versions
        @"Software\1C\1cv8",
    ];

    public RegistryMruReader(
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
        // Seed seen set without emitting events on first pass
        ScanMruKeys(emit: false);
        _timer = new Timer(_ => ScanMruKeys(emit: true), null,
            TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30));
    }

    private void ScanMruKeys(bool emit)
    {
        try
        {
            using var hkcu = Registry.CurrentUser;

            // Explorer RecentDocs
            using var recent = hkcu.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Explorer\RecentDocs");
            if (recent != null)
                ProcessMruKey(recent, "Registry:ExplorerMRU", emit);

            // Office MRU — enumerate all installed versions
            using var office = hkcu.OpenSubKey(@"Software\Microsoft\Office");
            if (office != null)
                foreach (var ver in office.GetSubKeyNames())
                    foreach (var app in new[] { "Word", "Excel", "PowerPoint", "Access" })
                    {
                        using var mru = office.OpenSubKey($@"{ver}\{app}\File MRU");
                        if (mru != null)
                            ProcessMruKey(mru, $"Registry:Office/{app}", emit);
                    }

            // 1C if present
            using var c1 = hkcu.OpenSubKey(@"Software\1C\1cv8");
            if (c1 != null)
                ProcessMruKey(c1, "Registry:1C", emit);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("MRU scan error: {Msg}", ex.Message);
        }
    }

    private void ProcessMruKey(RegistryKey key, string source, bool emit)
    {
        foreach (var name in key.GetValueNames())
        {
            var val = key.GetValue(name)?.ToString();
            if (string.IsNullOrEmpty(val)) continue;

            // Values may be binary (shell link) or string; use raw string representation
            var docName = ExtractDocName(val);
            if (string.IsNullOrEmpty(docName)) continue;

            var key_ = source + ":" + docName;
            if (!_seen.Add(key_)) continue;

            if (!emit) continue;

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
                EventType    = nameof(EventType.RecentDocumentOpened),
                LogSource    = source,
                DocumentName = docName,
                DocumentPath = val.Length > 500 ? val[..500] : val,
            });
        }
    }

    private static string? ExtractDocName(string raw)
    {
        // Office MRU format: "path\to\file.xlsx" or "C:\...\file.xlsx"
        try
        {
            var clean = raw.TrimStart('*').Split('\0')[0].Trim();
            if (clean.Length == 0) return null;
            return Path.GetFileName(clean);
        }
        catch { return null; }
    }

    public void Dispose() => _timer?.Dispose();
}
