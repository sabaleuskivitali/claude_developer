using System.Security.Cryptography;
using System.Text;
using System.Windows.Automation;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Capture;

/// <summary>
/// Best-effort UIAutomation capture. Events are written even if ElementName is null.
/// Password fields: event written without hash and without triggering screenshot.
/// </summary>
public sealed class UiAutomationCapture : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<UiAutomationCapture> _logger;

    internal Action<string>? OnUiEvent;   // wired by ScreenshotWorker

    private AutomationEventHandler? _invokeHandler;
    private AutomationPropertyChangedEventHandler? _valueHandler;
    private AutomationPropertyChangedEventHandler? _selectionHandler;
    private AutomationEventHandler? _focusHandler;

    // ValueChanged debounce: screenshot fires N ms after the last keystroke
    private Timer? _valueDebounceTimer;
    private readonly object _valueLock = new();

    // SelectionChanged throttle: screenshot no more than once per N ms
    private DateTime _lastSelectionCapture = DateTime.MinValue;

    public UiAutomationCapture(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<UiAutomationCapture> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override Task ExecuteAsync(CancellationToken ct)
    {
        try
        {
            _invokeHandler = (src, _) => HandleInvoke(src);
            Automation.AddAutomationEventHandler(
                InvokePattern.InvokedEvent,
                AutomationElement.RootElement,
                TreeScope.Descendants,
                _invokeHandler);

            _valueHandler = (src, args) => HandleValueChanged(src, args);
            Automation.AddAutomationPropertyChangedEventHandler(
                AutomationElement.RootElement,
                TreeScope.Descendants,
                _valueHandler,
                ValuePattern.ValueProperty);

            _selectionHandler = (src, args) => HandleSelectionChanged(src, args);
            Automation.AddAutomationPropertyChangedEventHandler(
                AutomationElement.RootElement,
                TreeScope.Descendants,
                _selectionHandler,
                SelectionItemPattern.IsSelectedProperty);

            _focusHandler = (src, _) => HandleFocus(src);
            Automation.AddAutomationEventHandler(
                AutomationElement.AutomationFocusChangedEvent,
                AutomationElement.RootElement,
                TreeScope.Descendants,
                _focusHandler);
        }
        catch (Exception ex)
        {
            WriteLayerError(ex);
        }

        return Task.Delay(Timeout.Infinite, ct);
    }

    public override Task StopAsync(CancellationToken ct)
    {
        try { Automation.RemoveAllEventHandlers(); } catch { }
        lock (_valueLock) { _valueDebounceTimer?.Dispose(); }
        return base.StopAsync(ct);
    }

    private void HandleInvoke(object src)
    {
        try
        {
            var el = (AutomationElement)src;
            var ev = BuildEvent(el, nameof(EventType.Invoked), null, null);
            if (ev is null) return;
            _store.Insert(ev);
            if (!ev.IsPasswordField)
                OnUiEvent?.Invoke("ui_event");
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private void HandleValueChanged(object src, AutomationPropertyChangedEventArgs args)
    {
        try
        {
            var el  = (AutomationElement)src;
            var val = args.NewValue?.ToString();
            var ev  = BuildEvent(el, nameof(EventType.ValueChanged), val, null);
            if (ev is null) return;
            _store.Insert(ev);

            if (!ev.IsPasswordField)
            {
                // Debounce: reset timer on every keystroke, screenshot fires after silence
                var debounceMs = _settings.CaptureProfile.ValueChangedDebounceMs;
                lock (_valueLock)
                {
                    _valueDebounceTimer?.Dispose();
                    _valueDebounceTimer = new Timer(
                        _ => OnUiEvent?.Invoke("field_value_settled"),
                        null, debounceMs, Timeout.Infinite);
                }
            }
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private void HandleSelectionChanged(object src, AutomationPropertyChangedEventArgs args)
    {
        try
        {
            var el  = (AutomationElement)src;
            var val = args.NewValue?.ToString();
            var ev  = BuildEvent(el, nameof(EventType.SelectionChanged), val, null);
            if (ev is null) return;
            _store.Insert(ev);

            if (!ev.IsPasswordField)
            {
                // Throttle: screenshot no more than once per SelectionChangedDebounceMs
                var now = DateTime.UtcNow;
                if ((now - _lastSelectionCapture).TotalMilliseconds >=
                    _settings.CaptureProfile.SelectionChangedDebounceMs)
                {
                    OnUiEvent?.Invoke("ui_event");
                    _lastSelectionCapture = now;
                }
            }
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private void HandleFocus(object src)
    {
        try
        {
            var el = (AutomationElement)src;
            var ev = BuildEvent(el, nameof(EventType.TextCommitted), null, null);
            if (ev is null) return;
            _store.Insert(ev);
            if (!ev.IsPasswordField)
                OnUiEvent?.Invoke("text_committed");
        }
        catch (Exception ex) { WriteLayerError(ex); }
    }

    private ActivityEvent? BuildEvent(AutomationElement el, string eventType, string? rawValue, string? captureReason)
    {
        AutomationElement.AutomationElementInformation info;
        try { info = el.Current; }
        catch { return null; }

        var isPassword  = info.IsPassword;
        var controlType = info.ControlType?.LocalizedControlType;

        string? valueHash = null;
        string? valueType = null;
        var valueLen = 0;

        if (!isPassword && rawValue != null)
        {
            valueLen  = rawValue.Length;
            valueType = ClassifyValue(rawValue);
            valueHash = valueLen > 0
                ? Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(rawValue))).ToLower()
                : null;
        }

        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        return new ActivityEvent
        {
            SessionId           = _store.SessionId,
            MachineId           = _settings.MachineId,
            UserId              = _settings.UserId,
            TimestampUtc        = raw,
            SyncedTs            = _ntp.SyncedTs(raw),
            DriftMs             = _ntp.CurrentDriftMs,
            DriftRatePpm        = _ntp.DriftRatePpm,
            Layer               = "window",
            EventType           = eventType,
            ProcessName         = TryGetProcessName(info.ProcessId),
            WindowTitle         = info.Name,
            WindowClass         = controlType ?? "",
            ElementType         = controlType,
            ElementName         = info.Name,
            ElementAutomationId = info.AutomationId,
            IsPasswordField     = isPassword,
            InputValueHash      = valueHash,
            InputValueType      = valueType,
            InputValueLength    = valueLen,
            CaptureReason       = captureReason,
        };
    }

    private static string ClassifyValue(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "EMPTY";
        if (double.TryParse(value, out _))     return "NUMBER";
        if (DateTime.TryParse(value, out _))   return "DATE";
        if (value.Contains('@'))               return "EMAIL";
        return "TEXT";
    }

    private static string TryGetProcessName(int pid)
    {
        try { return System.Diagnostics.Process.GetProcessById(pid).ProcessName; }
        catch { return ""; }
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
}
