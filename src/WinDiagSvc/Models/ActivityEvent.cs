using System.Text.Json;
using System.Text.Json.Serialization;

namespace WinDiagSvc.Models;

public sealed record ActivityEvent
{
    // Identity
    public Guid   EventId       { get; init; } = Guid.NewGuid();
    public Guid   SessionId     { get; init; }
    public string UserId        { get; init; } = "";
    public string MachineId     { get; init; } = "";
    public long   TimestampUtc  { get; init; } = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    public long   SyncedTs      { get; init; }
    public long   DriftMs       { get; init; }
    public double DriftRatePpm  { get; init; }
    public int    SequenceIndex { get; init; }

    // Layer and type
    public string Layer     { get; init; } = "";
    public string EventType { get; init; } = "";

    // Application context (always populated)
    public string ProcessName  { get; init; } = "";
    public string AppVersion   { get; init; } = "";
    public string WindowTitle  { get; init; } = "";
    public string WindowClass  { get; init; } = "";

    // UIAutomation element (nullable — often empty in corporate apps)
    public string? ElementType         { get; init; }
    public string? ElementName         { get; init; }
    public string? ElementAutomationId { get; init; }

    // Input data (no values — structure only)
    public string? InputValueHash   { get; init; }
    public string? InputValueType   { get; init; }
    public int     InputValueLength { get; init; }
    public bool    IsPasswordField  { get; init; }

    // Context flags
    public string? CaseIdCandidate      { get; init; }
    public bool    UndoPerformed        { get; init; }
    public bool    CopyPasteAcrossApps  { get; init; }
    public bool    ErrorDialogShown     { get; init; }
    public string? FileExtension        { get; init; }
    public string? FileOperation        { get; init; }

    // Screenshot (layer B)
    public string? ScreenshotPath  { get; init; }
    public ulong   ScreenshotDHash { get; init; }
    public string? CaptureReason   { get; init; }

    // Vision AI (populated by server)
    public bool    VisionDone       { get; init; }
    public bool    VisionSkipped    { get; init; }
    public string? VisionTaskLabel  { get; init; }
    public string? VisionAppContext { get; init; }
    public string? VisionActionType { get; init; }
    public string? VisionCaseId     { get; init; }
    public bool    VisionIsCommit   { get; init; }
    public string? VisionCognitive  { get; init; }
    public float   VisionConfidence { get; init; }
    public string? VisionAutoNotes  { get; init; }

    // Layer D — app logs
    public string? LogSource     { get; init; }
    public string? LogLevel      { get; init; }
    public string? RawMessage    { get; init; }
    public string? MessageHash   { get; init; }
    public string? DocumentPath  { get; init; }
    public string? DocumentName  { get; init; }

    // Layer E — browser
    public string? BrowserName        { get; init; }
    public string? BrowserUrl         { get; init; }
    public string? BrowserUrlPath     { get; init; }
    public string? BrowserPageTitle   { get; init; }
    public string? DomElementTag      { get; init; }
    public string? DomElementId       { get; init; }
    public string? DomElementName     { get; init; }
    public string? DomElementLabel    { get; init; }
    public string? DomFormAction      { get; init; }
    public int     DomFormFieldCount  { get; init; }
    public string? XhrMethod          { get; init; }
    public int     XhrStatus          { get; init; }

    // Per-layer health stats — populated only in HeartbeatPulse events
    public Dictionary<string, LayerStat>? LayerStats { get; init; }

    // Resolved case ID: Vision → regex → document name
    [JsonIgnore]
    public string? CaseId => VisionCaseId ?? CaseIdCandidate ?? DocumentName;

    // Full JSON payload — schema migration insurance
    public string Payload { get; init; } = "";

    public record LayerStat(int LastEventSec, int Events5Min, int Errors5Min, string Status);

    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    public string ToJson() => JsonSerializer.Serialize(this, _jsonOpts);
}
