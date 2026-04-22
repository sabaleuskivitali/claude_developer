using System.IO.Compression;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Capture;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;
using WinDiagSvc.Sync;

namespace WinDiagSvc.Management;

/// <summary>
/// Replaces UpdateManager. Polls GET /api/v1/updates/latest every UpdateCheckIntervalMinutes.
/// Downloads package via GET /api/v1/updates/{version}/package, verifies SHA256,
/// then hands off to WinDiagUpdater.ps1 via Scheduled Task.
/// </summary>
public sealed class HttpUpdateManager : BackgroundService
{
    private readonly EventStore      _store;
    private readonly NtpSynchronizer _ntp;
    private readonly ServerDiscovery _discovery;
    private readonly AgentSettings   _settings;
    private readonly ILogger<HttpUpdateManager> _logger;

    private static readonly Version _currentVersion =
        new(typeof(HttpUpdateManager).Assembly.GetName().Version?.ToString() ?? "1.0.0");

    private static readonly JsonSerializerOptions _jsonOpts = new()
        { PropertyNameCaseInsensitive = true };

    public HttpUpdateManager(
        EventStore store,
        NtpSynchronizer ntp,
        ServerDiscovery discovery,
        IOptions<AgentSettings> options,
        ILogger<HttpUpdateManager> logger)
    {
        _store     = store;
        _ntp       = ntp;
        _discovery = discovery;
        _settings  = options.Value;
        _logger    = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await CheckForUpdateAsync(ct);

        var interval = TimeSpan.FromMinutes(_settings.UpdateCheckIntervalMinutes);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
            await CheckForUpdateAsync(ct);
    }

    private async Task CheckForUpdateAsync(CancellationToken ct)
    {
        try
        {
            var url = await _discovery.GetServerUrlAsync(ct);
            if (url is null) return;

            using var req = new HttpRequestMessage(HttpMethod.Get, $"{url}/api/v1/updates/latest");
            req.Headers.Add("X-Api-Key", _settings.ApiKey);

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            if (resp.StatusCode == System.Net.HttpStatusCode.NotFound) return;
            resp.EnsureSuccessStatusCode();

            var manifest = await resp.Content.ReadFromJsonAsync<UpdateManifest>(_jsonOpts, ct);
            if (manifest is null) return;

            var latestVersion = new Version(manifest.Version);
            if (latestVersion <= _currentVersion) return;

            _logger.LogInformation("Update available: {Ver}", manifest.Version);
            WriteEvent(nameof(EventType.UpdateAvailable), manifest.Version);

            await ApplyUpdateAsync(url, manifest, ct);
        }
        catch (Exception ex)
        {
            _logger.LogWarning("HttpUpdateManager: {Msg}", ex.Message);
        }
    }

    private async Task ApplyUpdateAsync(string url, UpdateManifest manifest, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(
            HttpMethod.Get, $"{url}/api/v1/updates/{manifest.Version}/package");
        req.Headers.Add("X-Api-Key", _settings.ApiKey);

        using var resp = await _discovery.HttpClient.SendAsync(req, ct);
        resp.EnsureSuccessStatusCode();

        var zipBytes = await resp.Content.ReadAsByteArrayAsync(ct);

        var hash = Convert.ToHexString(SHA256.HashData(zipBytes)).ToLower();
        if (hash != manifest.Sha256.ToLower())
        {
            _logger.LogError("Update SHA256 mismatch — aborting");
            return;
        }

        var stagingDir = Path.Combine(AppContext.BaseDirectory, "staging");
        if (Directory.Exists(stagingDir))
            Directory.Delete(stagingDir, recursive: true);
        Directory.CreateDirectory(stagingDir);

        using (var zipStream = new MemoryStream(zipBytes))
            ZipFile.ExtractToDirectory(zipStream, stagingDir);

        _logger.LogInformation("Update staged: {Ver}", manifest.Version);
        WriteEvent(nameof(EventType.UpdateStarted), manifest.Version);

        var updaterScript = Path.Combine(AppContext.BaseDirectory, "WinDiagUpdater.ps1");
        var taskXml       = BuildTaskXml(updaterScript);
        var taskXmlPath   = Path.Combine(Path.GetTempPath(), "WinDiagUpdate.xml");
        await File.WriteAllTextAsync(taskXmlPath, taskXml, ct);

        var proc = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
        {
            FileName        = "schtasks.exe",
            Arguments       = $"/Create /XML \"{taskXmlPath}\" /TN \"WinDiagUpdate\" /F",
            UseShellExecute = false,
            CreateNoWindow  = true,
        });
        proc?.WaitForExit(5000);

        Environment.Exit(0);
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
            SessionId = _store.SessionId, MachineId = _settings.MachineId,
            UserId = _settings.UserId, TimestampUtc = raw,
            SyncedTs = _ntp.SyncedTs(raw), DriftMs = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm, Layer = "agent",
            EventType = eventType, AppVersion = version,
        });
    }

    private sealed class UpdateManifest
    {
        public string Version     { get; set; } = "";
        public string PackagePath { get; set; } = "";
        public string Sha256      { get; set; } = "";
        public string Changelog   { get; set; } = "";
    }
}
