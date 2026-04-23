namespace WinDiagSvc.Models;

public enum EventType
{
    // Layer A — window (always works)
    WindowActivated,
    AppSwitch,

    // UIAutomation (best-effort)
    Invoked,
    ValueChanged,
    SelectionChanged,
    TextCommitted,
    ClipboardCopy,
    ClipboardPaste,
    HotkeyUndo,
    ErrorDialogAppeared,
    IdleStart,
    IdleEnd,

    // Layer B — visual
    Screenshot,

    // Layer C — system
    ProcessStart,
    ProcessStop,
    FileCreate,
    FileWrite,

    // Layer D — app logs
    EventLogEntry,
    FileLogEntry,
    RecentDocumentOpened,
    LnkCreated,

    // Layer E — browser
    BrowserPageLoad,
    BrowserNavigation,
    BrowserTabActivated,
    BrowserFormFieldFocus,
    BrowserFormFieldBlur,
    BrowserElementClick,
    BrowserXhrRequest,
    BrowserFormSubmit,

    // Agent service events
    HeartbeatPulse,
    SyncCompleted,
    LayerError,
    CommandReceived,
    CommandExecuted,
    UpdateAvailable,
    UpdateStarted,
    UpdateCompleted,
    PerformanceSnapshot,

    // Layer health
    LayerStuck,      // emitted when a layer stops sending events beyond threshold
    LayerRestarted,  // emitted just before process restart triggered by watchdog or command

    // Added by server
    VisionContextAdded,
    TaskBoundaryDetected,
}
