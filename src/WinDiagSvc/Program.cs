using Microsoft.Extensions.Logging.Configuration;
using WinDiagSvc.Capture;
using WinDiagSvc.Capture.AppLogScanner;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;
using WinDiagSvc.Sync;
using WinDiagSvc.Management;
using WinDiagSvc.Browser;

var builder = Host.CreateApplicationBuilder(args);

builder.Services.Configure<AgentSettings>(
    builder.Configuration.GetSection("AgentSettings"));

// Storage — singleton: one connection for all layers
builder.Services.AddSingleton<EventStore>();

// NTP — singleton: shared drift state
builder.Services.AddSingleton<NtpSynchronizer>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<NtpSynchronizer>());

// Capture layers
builder.Services.AddHostedService<WindowWatcher>();
builder.Services.AddHostedService<ScreenshotWorker>();
builder.Services.AddHostedService<UiAutomationCapture>();
builder.Services.AddHostedService<ClipboardMonitor>();
builder.Services.AddHostedService<IdleDetector>();
builder.Services.AddHostedService<ProcessWatcher>();
builder.Services.AddHostedService<FileEventCapture>();

// Layer D — app log scanner (sub-services wired internally)
builder.Services.AddHostedService<AppLogScannerHost>();

// Layer E — browser native messaging
builder.Services.AddHostedService<BrowserMessageHost>();

// Sync and management
builder.Services.AddHostedService<FileSyncWorker>();
builder.Services.AddHostedService<HeartbeatWorker>();
builder.Services.AddHostedService<CommandPoller>();
builder.Services.AddHostedService<UpdateManager>();
builder.Services.AddHostedService<PerformanceMonitor>();

// File logging only — no console in service mode
builder.Logging.ClearProviders();
builder.Logging.AddConfiguration(builder.Configuration.GetSection("Logging"));

var logDir = builder.Configuration
    .GetSection("AgentSettings")
    .GetValue<string>("LogDir") ?? @"%ProgramData%\Microsoft\Diagnostics\logs";
logDir = Environment.ExpandEnvironmentVariables(logDir);
Directory.CreateDirectory(logDir);

builder.Logging.AddFile(Path.Combine(logDir, "agent-.log"),
    fileSizeLimitBytes: 10 * 1024 * 1024,
    retainedFileCountLimit: 5,
    rollOnFileSizeLimit: true);

var host = builder.Build();

// Ensure MachineId / UserId are persisted before any layer starts
using (var scope = host.Services.CreateScope())
{
    var settings = scope.ServiceProvider
        .GetRequiredService<Microsoft.Extensions.Options.IOptions<AgentSettings>>().Value;
    EnsureIdentity(settings);
}

host.UseWindowsService(o => o.ServiceName = "WinDiagSvc");

await host.RunAsync();

// ---------------------------------------------------------------------------

static void EnsureIdentity(AgentSettings settings)
{
    if (!string.IsNullOrEmpty(settings.MachineId) && !string.IsNullOrEmpty(settings.UserId))
        return;

    var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
    if (!File.Exists(configPath)) return;

    var json = File.ReadAllText(configPath);
    using var doc = System.Text.Json.JsonDocument.Parse(json);
    var root = doc.RootElement;

    var machineId = EventStore.ComputeId(Environment.MachineName);
    var userId    = EventStore.ComputeId(Environment.UserName + Environment.MachineName);

    // Patch the JSON section in-place (simple string replace — no full rewrite needed)
    var patched = json
        .Replace(@"""MachineId"": """"", $@"""MachineId"": ""{machineId}""")
        .Replace(@"""UserId"": """"",    $@"""UserId"": ""{userId}""");

    if (patched != json)
        File.WriteAllText(configPath, patched);

    settings.MachineId = machineId;
    settings.UserId    = userId;
}
