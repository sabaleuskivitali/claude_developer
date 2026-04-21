using NReco.Logging.File;
using WinDiagSvc.Capture;
using WinDiagSvc.Capture.AppLogScanner;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;
using WinDiagSvc.Sync;
using WinDiagSvc.Management;
using WinDiagSvc.Browser;

// UseWindowsService must be on the builder, before Build()
var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = "WinDiagSvc");

builder.Services.Configure<AgentSettings>(
    builder.Configuration.GetSection("AgentSettings"));

// Storage — singleton: one connection for all layers
builder.Services.AddSingleton<EventStore>();

// NTP — singleton: shared drift state
builder.Services.AddSingleton<NtpSynchronizer>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<NtpSynchronizer>());

// Capture layers — WindowWatcher, ScreenshotWorker, UiAutomationCapture registered as
// singletons so we can resolve them by type after Build() to wire screenshot triggers.
builder.Services.AddSingleton<WindowWatcher>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<WindowWatcher>());
builder.Services.AddSingleton<ScreenshotWorker>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<ScreenshotWorker>());
builder.Services.AddSingleton<UiAutomationCapture>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<UiAutomationCapture>());
builder.Services.AddHostedService<ClipboardMonitor>();
builder.Services.AddHostedService<IdleDetector>();
builder.Services.AddHostedService<ProcessWatcher>();
builder.Services.AddHostedService<FileEventCapture>();

// Layer D — app log scanner
builder.Services.AddHostedService<AppLogScannerHost>();

// Layer E — browser native messaging
builder.Services.AddHostedService<BrowserMessageHost>();

// Sync and management
builder.Services.AddHostedService<FileSyncWorker>();
builder.Services.AddHostedService<HeartbeatWorker>();
builder.Services.AddHostedService<CommandPoller>();
builder.Services.AddHostedService<UpdateManager>();
builder.Services.AddHostedService<PerformanceMonitor>();

// File logging — no console output in service mode
builder.Logging.ClearProviders();

var logDir = builder.Configuration
    .GetSection("AgentSettings")
    .GetValue<string>("LogDir") ?? @"%ProgramData%\Microsoft\Diagnostics\logs";
logDir = Environment.ExpandEnvironmentVariables(logDir);
Directory.CreateDirectory(logDir);

builder.Logging.AddFile(Path.Combine(logDir, "agent-.log"), o =>
{
    o.Append            = true;
    o.FileSizeLimitBytes = 10 * 1024 * 1024;
    o.MaxRollingFiles   = 5;
});

var host = builder.Build();

// Ensure MachineId / UserId are persisted before any layer starts
using (var scope = host.Services.CreateScope())
{
    var settings = scope.ServiceProvider
        .GetRequiredService<Microsoft.Extensions.Options.IOptions<AgentSettings>>().Value;
    EnsureIdentity(settings);
}

// Wire screenshot triggers between layers (fixes CS0649 warnings)
var ww  = host.Services.GetRequiredService<WindowWatcher>();
var sw  = host.Services.GetRequiredService<ScreenshotWorker>();
var uia = host.Services.GetRequiredService<UiAutomationCapture>();
ww.OnWindowChanged = _ => sw.TriggerCapture("window_activated");
uia.OnUiEvent      = _ => sw.TriggerCapture("ui_event");

await host.RunAsync();

// ---------------------------------------------------------------------------

static void EnsureIdentity(AgentSettings settings)
{
    if (!string.IsNullOrEmpty(settings.MachineId) && !string.IsNullOrEmpty(settings.UserId))
        return;

    var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
    if (!File.Exists(configPath)) return;

    var json = File.ReadAllText(configPath);

    var machineId = EventStore.ComputeId(Environment.MachineName);
    var userId    = EventStore.ComputeId(Environment.UserName + Environment.MachineName);

    var patched = json
        .Replace(@"""MachineId"": """"", $@"""MachineId"": ""{machineId}""")
        .Replace(@"""UserId"": """"",    $@"""UserId"": ""{userId}""");

    if (patched != json)
        File.WriteAllText(configPath, patched);

    settings.MachineId = machineId;
    settings.UserId    = userId;
}
