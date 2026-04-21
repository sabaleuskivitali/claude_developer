using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Microsoft.Extensions.Logging;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture.AppLogScanner;

/// <summary>
/// Watches %AppData%\Microsoft\Windows\Recent\ for new LNK files.
/// Parses the target path using IShellLink COM — gives instant document-open events
/// with precise timestamps even when UIAutomation returns nothing.
/// </summary>
public sealed class LnkWatcher : IDisposable
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger _logger;

    private FileSystemWatcher? _watcher;

    public LnkWatcher(
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
        var recentDir = Environment.GetFolderPath(Environment.SpecialFolder.Recent);
        if (!Directory.Exists(recentDir)) return;

        _watcher = new FileSystemWatcher(recentDir, "*.lnk")
        {
            NotifyFilter            = NotifyFilters.FileName | NotifyFilters.LastWrite,
            IncludeSubdirectories   = false,
            EnableRaisingEvents     = true,
        };
        _watcher.Created += (_, e) => HandleLnk(e.FullPath);
        _watcher.Changed += (_, e) => HandleLnk(e.FullPath);
    }

    private void HandleLnk(string lnkPath)
    {
        try
        {
            var target = ResolveLnkTarget(lnkPath);
            if (string.IsNullOrEmpty(target)) return;

            var docName = Path.GetFileName(target);
            var raw     = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

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
                EventType    = nameof(EventType.LnkCreated),
                LogSource    = "LNK",
                DocumentPath = target.Length > 500 ? target[..500] : target,
                DocumentName = docName,
            });
        }
        catch (Exception ex)
        {
            _logger.LogDebug("LNK parse error {Path}: {Msg}", lnkPath, ex.Message);
        }
    }

    private static string? ResolveLnkTarget(string lnkPath)
    {
        // IShellLink COM object
        var shellLink = (IShellLinkW)new ShellLink();
        var persistFile = (IPersistFile)shellLink;

        persistFile.Load(lnkPath, 0 /* STGM_READ */);

        var buf = new char[260];
        WIN32_FIND_DATAW fd = default;
        shellLink.GetPath(buf, buf.Length, ref fd, SLGP_RAWPATH);

        var path = new string(buf).TrimEnd('\0');
        return string.IsNullOrEmpty(path) ? null : path;
    }

    public void Dispose() => _watcher?.Dispose();

    // -----------------------------------------------------------------------
    // COM interop for IShellLink
    // -----------------------------------------------------------------------

    private const uint SLGP_RAWPATH = 0x0004;

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] char[] pszFile, int cch, ref WIN32_FIND_DATAW pfd, uint fFlags);
        void GetIDList(out nint ppidl);
        void SetIDList(nint pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] char[] pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] char[] pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] char[] pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out ushort pwHotkey);
        void SetHotkey(ushort wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] char[] pszIconPath, int cch, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(nint hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WIN32_FIND_DATAW
    {
        public uint dwFileAttributes;
        public FILETIME ftCreationTime, ftLastAccessTime, ftLastWriteTime;
        public uint nFileSizeHigh, nFileSizeLow, dwReserved0, dwReserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string cFileName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]  public string cAlternateFileName;
    }
}
