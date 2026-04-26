using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Seamlean.Agent.Capture.Meeting;

/// <summary>
/// Polls every 5 seconds for meeting processes and window titles.
/// Calls MeetingRecordingService when a meeting starts or ends.
/// Does not depend on WindowWatcher internals.
/// </summary>
public sealed class MeetingDetector : BackgroundService
{
    // Process names that host audio calls (compare case-insensitively, without .exe)
    private static readonly HashSet<string> MeetingProcesses =
        new(StringComparer.OrdinalIgnoreCase)
        {
            "Teams", "ms-teams", "Zoom", "CiscoWebex", "webex", "slack",
            "GoogleTalkPluginD", "meet",
        };

    // Window title substrings that indicate an active call
    private static readonly string[] CallTitleKeywords =
    [
        "In a call", "in a meeting", "Meeting", "звонок", "конференция",
        "Call in progress", "On a call",
    ];

    // Window title substrings that indicate Google Meet or Zoom in browser
    private static readonly string[] BrowserMeetingUrlHints =
    [
        "meet.google.com", "zoom.us/j/", "teams.microsoft.com/meeting",
        "webex.com/meet",
    ];

    private const int PollIntervalMs    = 5_000;
    private const int ConfirmStopCycles = 3; // require N consecutive non-meeting polls before stopping

    private readonly MeetingRecordingService _recorder;
    private readonly ILogger<MeetingDetector> _logger;

    private int _stopConfirmCount;

    public MeetingDetector(MeetingRecordingService recorder, ILogger<MeetingDetector> logger)
    {
        _recorder = recorder;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(PollIntervalMs));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await CheckAsync(); }
            catch (Exception ex) { _logger.LogDebug("MeetingDetector: {Msg}", ex.Message); }
        }
    }

    private async Task CheckAsync()
    {
        var (inMeeting, trigger, processName, windowTitle) = DetectMeeting();

        if (inMeeting)
        {
            _stopConfirmCount = 0;
            if (!_recorder.IsRecording)
                await _recorder.StartRecordingAsync(trigger!, processName, windowTitle);
        }
        else if (_recorder.IsRecording)
        {
            _stopConfirmCount++;
            if (_stopConfirmCount >= ConfirmStopCycles)
            {
                _stopConfirmCount = 0;
                await _recorder.StopRecordingAsync("no_meeting_detected");
            }
        }
    }

    private static (bool, string?, string?, string?) DetectMeeting()
    {
        // 1. Check foreground window — works for both desktop apps and browsers
        var foregroundTitle = GetForegroundWindowTitle();
        if (!string.IsNullOrEmpty(foregroundTitle))
        {
            foreach (var hint in BrowserMeetingUrlHints)
                if (foregroundTitle.Contains(hint, StringComparison.OrdinalIgnoreCase))
                    return (true, "browser_url", null, foregroundTitle);

            foreach (var kw in CallTitleKeywords)
                if (foregroundTitle.Contains(kw, StringComparison.OrdinalIgnoreCase))
                {
                    var (proc, _) = GetForegroundProcess();
                    return (true, "window_title", proc, foregroundTitle);
                }
        }

        // 2. Check all windows of known meeting processes for call-indicating titles
        foreach (var proc in Process.GetProcesses())
        {
            if (!MeetingProcesses.Contains(proc.ProcessName)) continue;
            if (proc.MainWindowHandle == nint.Zero) continue;

            var title = proc.MainWindowTitle;
            if (string.IsNullOrEmpty(title)) continue;

            foreach (var kw in CallTitleKeywords)
                if (title.Contains(kw, StringComparison.OrdinalIgnoreCase))
                    return (true, "process_title", proc.ProcessName, title);

            foreach (var hint in BrowserMeetingUrlHints)
                if (title.Contains(hint, StringComparison.OrdinalIgnoreCase))
                    return (true, "process_url", proc.ProcessName, title);
        }

        return (false, null, null, null);
    }

    private static string GetForegroundWindowTitle()
    {
        var hwnd = GetForegroundWindow();
        if (hwnd == nint.Zero) return "";
        var sb = new StringBuilder(512);
        GetWindowText(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static (string name, string title) GetForegroundProcess()
    {
        var hwnd = GetForegroundWindow();
        if (hwnd == nint.Zero) return ("", "");
        GetWindowThreadProcessId(hwnd, out var pid);
        try
        {
            using var p = Process.GetProcessById((int)pid);
            return (p.ProcessName, p.MainWindowTitle);
        }
        catch { return ("", ""); }
    }

    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern int GetWindowText(nint hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hWnd, out uint pid);
}
