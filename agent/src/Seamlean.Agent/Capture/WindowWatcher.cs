using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture;

public sealed class WindowWatcher : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<WindowWatcher> _logger;

    // Injected by ScreenshotWorker via internal channel
    internal Action<string>? OnWindowChanged;

    private string _lastProcessName = "";
    private nint   _hookHandle;
    private nint   _dialogHookHandle;

    // Callback must be kept alive for the duration of the hook
    private WinEventDelegate? _hookProc;

    public WindowWatcher(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<WindowWatcher> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override Task ExecuteAsync(CancellationToken ct)
    {
        // WinEventHook is thread-affine: the hook and its message pump must run on the same thread.
        // Task.Delay with ConfigureAwait(false) would switch threads after each await, causing
        // the message pump to run on a different thread — so WinEvent messages would never be delivered.
        // Fix: run a dedicated STA message-pump thread for the lifetime of the service.
        var thread = new Thread(() => RunMessageLoop(ct))
        {
            IsBackground = true,
            Name         = "WindowWatcher.MsgPump",
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        return Task.CompletedTask;
    }

    private void RunMessageLoop(CancellationToken ct)
    {
        try
        {
            _hookProc = OnWinEvent;
            _hookHandle = SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                nint.Zero, _hookProc,
                0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);

            if (_hookHandle == nint.Zero)
            {
                _logger.LogError("SetWinEventHook failed (LastError={E})", Marshal.GetLastWin32Error());
                return;
            }

            _dialogHookHandle = SetWinEventHook(
                EVENT_SYSTEM_DIALOGSTART, EVENT_SYSTEM_DIALOGSTART,
                nint.Zero, _hookProc,
                0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);

            // Block with a 16ms timeout to stay responsive to cancellation
            // while keeping the pump on this thread at all times.
            while (!ct.IsCancellationRequested)
            {
                MsgWaitForMultipleObjectsEx(0, nint.Zero, 16, QS_ALLEVENTS, MWMO_INPUTAVAILABLE);
                while (PeekMessage(out var msg, nint.Zero, 0, 0, PM_REMOVE))
                {
                    TranslateMessage(ref msg);
                    DispatchMessage(ref msg);
                }
            }
        }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }
        finally
        {
            if (_hookHandle != nint.Zero)
                UnhookWinEvent(_hookHandle);
            if (_dialogHookHandle != nint.Zero)
                UnhookWinEvent(_dialogHookHandle);
        }
    }

    private void OnWinEvent(
        nint hWinEventHook, uint eventType, nint hwnd,
        int idObject, int idChild, uint dwEventThread, uint dwmsEventTime)
    {
        try
        {
            if (hwnd == nint.Zero) return;

            if (eventType == EVENT_SYSTEM_DIALOGSTART)
            {
                HandleDialogStart(hwnd);
                return;
            }

            var title   = GetTitle(hwnd);
            var cls     = GetClass(hwnd);
            var (name, version) = GetProcess(hwnd);
            if (string.IsNullOrEmpty(name)) return;

            var isSwitch = name != _lastProcessName;
            _lastProcessName = name;

            var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var ev = new ActivityEvent
            {
                SessionId     = _store.SessionId,
                MachineId     = _settings.MachineId,
                UserId        = _settings.UserId,
                TimestampUtc  = raw,
                SyncedTs      = _ntp.SyncedTs(raw),
                DriftMs       = _ntp.CurrentDriftMs,
                DriftRatePpm  = _ntp.DriftRatePpm,
                Layer         = "window",
                EventType     = isSwitch ? nameof(EventType.AppSwitch) : nameof(EventType.WindowActivated),
                ProcessName   = name,
                AppVersion    = version,
                WindowTitle   = title,
                WindowClass   = cls,
                CaptureReason = "window_activated",
            };

            _store.Insert(ev);
            OnWindowChanged?.Invoke("window_activated");
        }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }
    }

    private void HandleDialogStart(nint hwnd)
    {
        var title = GetTitle(hwnd);
        var (name, version) = GetProcess(hwnd);

        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            SessionId     = _store.SessionId,
            MachineId     = _settings.MachineId,
            UserId        = _settings.UserId,
            TimestampUtc  = raw,
            SyncedTs      = _ntp.SyncedTs(raw),
            DriftMs       = _ntp.CurrentDriftMs,
            DriftRatePpm  = _ntp.DriftRatePpm,
            Layer         = "window",
            EventType     = nameof(EventType.WindowActivated),
            ProcessName   = name,
            AppVersion    = version,
            WindowTitle   = title,
            WindowClass   = "#32770",   // standard dialog class
            CaptureReason = "dialog_appeared",
        });
        OnWindowChanged?.Invoke("dialog_appeared");
    }

    private static string GetTitle(nint hwnd)
    {
        var sb = new StringBuilder(512);
        GetWindowText(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static string GetClass(nint hwnd)
    {
        var sb = new StringBuilder(256);
        GetClassName(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static (string name, string version) GetProcess(nint hwnd)
    {
        GetWindowThreadProcessId(hwnd, out var pid);
        try
        {
            using var proc = Process.GetProcessById((int)pid);
            var name    = proc.ProcessName;
            var version = "";
            try
            {
                var exePath = proc.MainModule?.FileName;
                if (exePath != null)
                    version = FileVersionInfo.GetVersionInfo(exePath).ProductVersion ?? "";
            }
            catch { /* MainModule access may throw for protected processes */ }
            return (name, version);
        }
        catch { return ("", ""); }
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
            Layer        = "window",
            EventType    = nameof(EventType.LayerError),
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });

    // -----------------------------------------------------------------------
    // P/Invoke
    // -----------------------------------------------------------------------

    private const uint EVENT_SYSTEM_FOREGROUND  = 0x0003;
    private const uint EVENT_SYSTEM_DIALOGSTART = 0x0010;
    private const uint WINEVENT_OUTOFCONTEXT   = 0x0000;
    private const uint WINEVENT_SKIPOWNPROCESS = 0x0002;
    private const uint PM_REMOVE        = 0x0001;
    private const uint QS_ALLEVENTS     = 0x04BF;
    private const uint MWMO_INPUTAVAILABLE = 0x0004;

    private delegate void WinEventDelegate(
        nint hWinEventHook, uint eventType, nint hwnd,
        int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

    [DllImport("user32.dll")]
    private static extern nint SetWinEventHook(
        uint eventMin, uint eventMax, nint hmodWinEventProc,
        WinEventDelegate lpfnWinEventProc, uint idProcess, uint idThread, uint dwFlags);

    [DllImport("user32.dll")]
    private static extern bool UnhookWinEvent(nint hWinEventHook);

    [DllImport("user32.dll")]
    private static extern int GetWindowText(nint hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    private static extern int GetClassName(nint hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(nint hWnd, out uint lpdwProcessId);

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG { public nint hwnd; public uint message; public nint wParam; public nint lParam; public uint time; public int ptX, ptY; }

    [DllImport("user32.dll")]
    private static extern bool PeekMessage(out MSG lpMsg, nint hWnd, uint wMsgFilterMin, uint wMsgFilterMax, uint wRemoveMsg);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern nint DispatchMessage(ref MSG lpmsg);

    [DllImport("user32.dll")]
    private static extern uint MsgWaitForMultipleObjectsEx(
        uint nCount, nint pHandles, uint dwMilliseconds, uint dwWakeMask, uint dwFlags);
}
