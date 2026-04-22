namespace WinDiagSvc.Models;

public sealed class AgentSettings
{
    public string MachineId { get; set; } = "";
    public string UserId    { get; set; } = "";

    public string DbPath        { get; set; } = @"%ProgramData%\Microsoft\Diagnostics\events.db";
    public string ScreenshotDir { get; set; } = @"%ProgramData%\Microsoft\Diagnostics\cache";
    public string LogDir        { get; set; } = @"%ProgramData%\Microsoft\Diagnostics\logs";

    // HTTP API
    public string ServerUrl        { get; set; } = "";  // empty = autodiscovery
    public string ApiKey           { get; set; } = "";
    public string ServerThumbprint { get; set; } = "";  // SHA256 hex, filled on first connect

    // SMB share — filled by install.ps1
    public string SharePath { get; set; } = "";
    public string ShareUser { get; set; } = "";
    public string SharePass { get; set; } = "";

    public int SyncIntervalSeconds       { get; set; } = 30;
    public int ScreenshotIntervalSeconds { get; set; } = 10;
    public int DHashDistanceThreshold    { get; set; } = 10;
    public int IdleLightThresholdMs      { get; set; } = 30_000;
    public int IdleDeepThresholdMs       { get; set; } = 120_000;

    public string[] NtpServers         { get; set; } = ["pool.ntp.org", "time.windows.com", "time.nist.gov"];
    public int      NtpIntervalMinutes { get; set; } = 2;

    public int HeartbeatIntervalSeconds    { get; set; } = 60;
    public int PerformanceIntervalMinutes  { get; set; } = 5;
    public int CommandPollIntervalSeconds  { get; set; } = 60;
    public int UpdateCheckIntervalMinutes  { get; set; } = 10;

    public string[] FileExtensionsToTrack { get; set; } =
        [".doc", ".docx", ".xls", ".xlsx", ".pdf", ".csv", ".txt", ".xml", ".1cd", ".mxl", ".erf"];

    public CaseIdPattern[] CaseIdPatterns { get; set; } = [];

    public string ExtensionId       { get; set; } = "";
    public int    ExtensionHostPort { get; set; } = 9876;

    // LayerWatchdog — how often to check (minutes) and restart after N consecutive stuck cycles
    public int WatchdogIntervalMinutes { get; set; } = 2;
    public int WatchdogRestartCycles   { get; set; } = 2;

    public string ExpandedDbPath        => Environment.ExpandEnvironmentVariables(DbPath);
    public string ExpandedScreenshotDir => Environment.ExpandEnvironmentVariables(ScreenshotDir);
    public string ExpandedLogDir        => Environment.ExpandEnvironmentVariables(LogDir);
}

public sealed class CaseIdPattern
{
    public string ProcessName { get; set; } = "";
    public string Pattern     { get; set; } = "";
}
