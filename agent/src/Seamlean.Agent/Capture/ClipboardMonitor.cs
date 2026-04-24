using System.Runtime.InteropServices;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture;

/// <summary>
/// Monitors clipboard changes via AddClipboardFormatListener (hidden message window).
/// Detects cross-app paste by comparing active window at copy vs. paste time.
/// </summary>
public sealed class ClipboardMonitor : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<ClipboardMonitor> _logger;

    private string _lastCopyProcess = "";

    public ClipboardMonitor(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<ClipboardMonitor> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Create a hidden message-only window on a dedicated STA thread
        var tcs = new TaskCompletionSource();
        var thread = new Thread(() =>
        {
            try { RunMessageLoop(ct, tcs); }
            catch (Exception ex) { WriteLayerError(ex); tcs.TrySetResult(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.IsBackground = true;
        thread.Start();

        await tcs.Task;
    }

    private void RunMessageLoop(CancellationToken ct, TaskCompletionSource tcs)
    {
        var hwnd = CreateWindowEx(0, "STATIC", null,
            WS_POPUP, 0, 0, 0, 0, HWND_MESSAGE, nint.Zero, nint.Zero, nint.Zero);

        if (hwnd == nint.Zero) { tcs.TrySetResult(); return; }

        AddClipboardFormatListener(hwnd);

        ct.Register(() => PostMessage(hwnd, WM_QUIT, nint.Zero, nint.Zero));

        while (GetMessage(out var msg, nint.Zero, 0, 0) > 0)
        {
            if (msg.message == WM_CLIPBOARDUPDATE)
                HandleClipboardUpdate();

            TranslateMessage(ref msg);
            DispatchMessage(ref msg);
        }

        RemoveClipboardFormatListener(hwnd);
        DestroyWindow(hwnd);
        tcs.TrySetResult();
    }

    private void HandleClipboardUpdate()
    {
        try
        {
            var hwnd = GetForegroundWindow();
            GetWindowThreadProcessId(hwnd, out var pid);
            string processName;
            try { processName = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName; }
            catch { processName = ""; }

            // Detect if clipboard had text (simplistic — check CF_UNICODETEXT presence)
            var hasText = IsClipboardFormatAvailable(CF_UNICODETEXT);

            // Heuristic: if clipboard was set from a different process recently, this is a copy
            // Paste detection: same clipboard content was available when a new process became active
            var isCrossApp = !string.IsNullOrEmpty(_lastCopyProcess) && processName != _lastCopyProcess;
            var eventType  = hasText ? nameof(EventType.ClipboardCopy) : nameof(EventType.ClipboardPaste);

            // Track copy source process
            if (hasText) _lastCopyProcess = processName;

            var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            _store.Insert(new ActivityEvent
            {
                SessionId          = _store.SessionId,
                MachineId          = _settings.MachineId,
                UserId             = _settings.UserId,
                TimestampUtc       = raw,
                SyncedTs           = _ntp.SyncedTs(raw),
                DriftMs            = _ntp.CurrentDriftMs,
                DriftRatePpm       = _ntp.DriftRatePpm,
                Layer              = "window",
                EventType          = eventType,
                ProcessName        = processName,
                CopyPasteAcrossApps = isCrossApp,
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
            Layer        = "window",
            EventType    = nameof(EventType.LayerError),
            RawMessage   = ex.Message[..Math.Min(ex.Message.Length, 500)],
        });

    // -----------------------------------------------------------------------
    // P/Invoke
    // -----------------------------------------------------------------------

    private const uint WS_POPUP           = 0x80000000;
    private const uint WM_CLIPBOARDUPDATE  = 0x031D;
    private const uint WM_QUIT             = 0x0012;
    private const uint CF_UNICODETEXT      = 13;
    private static readonly nint HWND_MESSAGE = new(-3);

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG { public nint hwnd; public uint message; public nint wParam; public nint lParam; public uint time; public int ptX, ptY; }

    [DllImport("user32.dll")] private static extern nint CreateWindowEx(uint dwExStyle, string lpClassName, string? lpWindowName, uint dwStyle, int x, int y, int nWidth, int nHeight, nint hWndParent, nint hMenu, nint hInstance, nint lpParam);
    [DllImport("user32.dll")] private static extern bool DestroyWindow(nint hWnd);
    [DllImport("user32.dll")] private static extern bool AddClipboardFormatListener(nint hWnd);
    [DllImport("user32.dll")] private static extern bool RemoveClipboardFormatListener(nint hWnd);
    [DllImport("user32.dll")] private static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] private static extern int GetMessage(out MSG lpMsg, nint hWnd, uint wMsgFilterMin, uint wMsgFilterMax);
    [DllImport("user32.dll")] private static extern bool TranslateMessage(ref MSG lpMsg);
    [DllImport("user32.dll")] private static extern nint DispatchMessage(ref MSG lpmsg);
    [DllImport("user32.dll")] private static extern bool PostMessage(nint hWnd, uint Msg, nint wParam, nint lParam);
}
