using NReco.Logging.File;
using Seamlean.Agent.Bootstrap;
using Seamlean.Agent.Browser;
using Seamlean.Agent.Capture;
using Seamlean.Agent.Capture.AppLogScanner;
using Seamlean.Agent.Installer;
using Seamlean.Agent.Management;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;
using Seamlean.Agent.Sync;

// -----------------------------------------------------------------------
// Installer mode — run before anything else so no DI/SQLite is initialised.
// -----------------------------------------------------------------------
if (args.Contains("--install") || args.Contains("--uninstall"))
    return await Installer.RunAsync(args);

// -----------------------------------------------------------------------
// Native Messaging Host mode — launched by Chrome/Edge as subprocess.
// Detection: stdin is a pipe (FILE_TYPE_PIPE) not a console or null.
// Run a minimal raw loop — NO DI, NO SQLite, NO ETW/WMI.
// Messages are appended to a JSONL queue file; the main service imports them.
// -----------------------------------------------------------------------
if (NativeMessagingDetector.IsNativeMessagingHost())
{
    await NativeMessagingDetector.RunAsync();
    return 0;
}

// Snake_case column names → PascalCase properties (e.g. machine_id → MachineId)
Dapper.DefaultTypeMap.MatchNamesWithUnderscores = true;

// Microsoft.Data.Sqlite stores GUIDs as TEXT; Dapper needs a handler to parse them back.
Dapper.SqlMapper.AddTypeHandler(new GuidTypeHandler());

// UseWindowsService must be on the builder, before Build()
var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = "WinDiagSvc");

builder.Services.Configure<AgentSettings>(
    builder.Configuration.GetSection("AgentSettings"));

// Bootstrap — must run before any service that connects to the server
builder.Services.AddSingleton<CascadeResolver>();
builder.Services.AddSingleton<EnrollmentClient>();
builder.Services.AddSingleton<BootstrapService>();
builder.Services.AddHostedService<ReEnrollmentService>();

// Storage — singleton: one connection for all layers
builder.Services.AddSingleton<LayerHealthTracker>();
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

// Layer E — browser events via file queue (native host writes JSONL, importer reads)
builder.Services.AddHostedService<BrowserQueueImporter>();
// Localhost CRX host — serves extension.crx for non-domain machines
builder.Services.AddHostedService<ExtensionHostService>();

// Sync and management — HTTP API
builder.Services.AddSingleton<ServerDiscovery>();
builder.Services.AddSingleton<ErrorReporter>();
builder.Services.AddSingleton<LayerWatchdog>();
builder.Services.AddHostedService<HttpSyncWorker>();
builder.Services.AddHostedService<HeartbeatWorker>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<LayerWatchdog>());
builder.Services.AddHostedService<HttpCommandPoller>();
builder.Services.AddHostedService<HttpUpdateManager>();
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

// Run bootstrap cascade before any service connects to the server.
// If ServerUrl is already set in appsettings.json, this is a no-op.
using (var scope = host.Services.CreateScope())
{
    var bootstrap = scope.ServiceProvider.GetRequiredService<BootstrapService>();
    await bootstrap.RunBootstrapAsync();
}

// Wire screenshot triggers between layers (fixes CS0649 warnings)
var ww  = host.Services.GetRequiredService<WindowWatcher>();
var sw  = host.Services.GetRequiredService<ScreenshotWorker>();
var uia = host.Services.GetRequiredService<UiAutomationCapture>();
ww.OnWindowChanged = _ => sw.TriggerCapture("window_activated");
uia.OnUiEvent      = _ => sw.TriggerCapture("ui_event");

await host.RunAsync();
return 0;

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
        try { File.WriteAllText(configPath, patched); } catch { }

    settings.MachineId = machineId;
    settings.UserId    = userId;
}

// Microsoft.Data.Sqlite stores GUIDs as TEXT; Dapper needs a handler to parse them back.
sealed class GuidTypeHandler : Dapper.SqlMapper.TypeHandler<Guid>
{
    public override void SetValue(System.Data.IDbDataParameter parameter, Guid value)
        => parameter.Value = value.ToString();
    public override Guid Parse(object value)
        => Guid.Parse(value.ToString()!);
}
