using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Management;

/// <summary>
/// Checks {SharePath}\updates\latest.json every UpdateCheckIntervalMinutes.
/// If a newer version is available: downloads, verifies SHA256, stages files,
/// creates a Scheduled Task to run WinDiagUpdater.ps1, then stops the service.
/// The updater script runs outside the service process and replaces the exe.
/// </summary>
public sealed class UpdateManager : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<UpdateManager> _logger;

    private static readonly Version _currentVersion =
        new(typeof(UpdateManager).Assembly.GetName().Version?.ToString() ?? "1.0.0");

    private static readonly JsonSerializerOptions _jsonOpts = new()
        { PropertyNameCaseInsensitive = true };

    public UpdateManager(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<UpdateManager> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Check immediately at startup, then on interval
        await CheckForUpdateAsync();

        var interval = TimeSpan.FromMinutes(_settings.UpdateCheckIntervalMinutes);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
            await CheckForUpdateAsync();
    }

    private async Task CheckForUpdateAsync()
    {
        try
        {
            var latestPath = Path.Combine(_settings.SharePath, "updates", "latest.json");
            if (!File.Exists(latestPath)) return;

            var json     = await File.ReadAllTextAsync(latestPath);
            var manifest = JsonSerializer.Deserialize<UpdateManifest>(json, _jsonOpts);
            if (manifest is null) return;

            var latestVersion = new Version(manifest.Version);
            if (latestVersion <= _currentVersion) return;

            _logger.LogInformation("Update available: {Ver} (current: {Cur})",
                manifest.Version, _currentVersion);

            WriteEvent(nameof(EventType.UpdateAvailable), manifest.Version);
            await ApplyUpdateAsync(manifest);
        }
        catch (Exception ex)
        {
            _logger.LogWarning("UpdateManager check failed: {Msg}", ex.Message);
        }
    }

    private async Task ApplyUpdateAsync(UpdateManifest manifest)
    {
        var packagePath = Path.Combine(_settings.SharePath, manifest.PackagePath);
        if (!File.Exists(packagePath))
        {
            _logger.LogWarning("Update package not found: {Path}", packagePath);
            return;
        }

        // Verify SHA256
        var hash = Convert.ToHexString(
            SHA256.HashData(await File.ReadAllBytesAsync(packagePath))).ToLower();
        if (hash != manifest.Sha256.ToLower())
        {
            _logger.LogError("Update package SHA256 mismatch — aborting");
            return;
        }

        // Extract to staging
        var stagingDir = Path.Combine(AppContext.BaseDirectory, "staging");
        if (Directory.Exists(stagingDir))
            Directory.Delete(stagingDir, recursive: true);
        Directory.CreateDirectory(stagingDir);

        ZipFile.ExtractToDirectory(packagePath, stagingDir);

        _logger.LogInformation("Update staged to {Dir}", stagingDir);
        WriteEvent(nameof(EventType.UpdateStarted), manifest.Version);

        // Create Scheduled Task for the updater script
        var updaterScript = Path.Combine(AppContext.BaseDirectory, "WinDiagUpdater.ps1");
        var taskXml = BuildTaskXml(updaterScript);
        var taskXmlPath = Path.Combine(Path.GetTempPath(), "WinDiagUpdate.xml");
        await File.WriteAllTextAsync(taskXmlPath, taskXml);

        var result = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName        = "schtasks.exe",
            Arguments       = $"/Create /XML \"{taskXmlPath}\" /TN \"WinDiagUpdate\" /F",
            UseShellExecute = false,
            CreateNoWindow  = true,
        });
        result?.WaitForExit(5000);

        // Stop this service — the scheduled task will restart it after replacing the exe
        _logger.LogInformation("Handing off to WinDiagUpdater, stopping service");

        Environment.Exit(0);  // triggers service stop; updater picks up from here
    }

    private static string BuildTaskXml(string scriptPath) => $"""
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers>
            <TimeTrigger>
              <StartBoundary>{DateTime.UtcNow.AddSeconds(10):yyyy-MM-ddTHH:mm:ss}</StartBoundary>
              <Enabled>true</Enabled>
            </TimeTrigger>
          </Triggers>
          <Principals>
            <Principal>
              <UserId>S-1-5-18</UserId>
              <RunLevel>HighestAvailable</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
          </Settings>
          <Actions>
            <Exec>
              <Command>powershell.exe</Command>
              <Arguments>-ExecutionPolicy Bypass -NonInteractive -File "{scriptPath}"</Arguments>
            </Exec>
          </Actions>
        </Task>
        """;

    private void WriteEvent(string eventType, string version)
    {
        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            SessionId    = _store.SessionId,
            MachineId    = _settings.MachineId,
            UserId       = _settings.UserId,
            TimestampUtc = raw,
            SyncedTs     = _ntp.SyncedTs(raw),
            DriftMs      = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm,
            Layer        = "agent",
            EventType    = eventType,
            AppVersion   = version,
        });
    }

    // -----------------------------------------------------------------------
    // DTO
    // -----------------------------------------------------------------------

    private sealed class UpdateManifest
    {
        public string Version     { get; set; } = "";
        public string PackagePath { get; set; } = "";
        public string Sha256      { get; set; } = "";
        public string MinVersion  { get; set; } = "";
        public string Changelog   { get; set; } = "";
    }
}
