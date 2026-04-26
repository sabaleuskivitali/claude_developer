using System.Net.Http;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Sync;

/// <summary>
/// Uploads pending screenshots to /api/v1/screenshots/{machine}/{date}/{event_id}
/// as raw binary (Content-Type: image/webp). Tracks upload status via screenshot_sent column:
/// 0=pending, 1=uploaded, 2=skipped (file missing or permanent error).
/// Runs independently of HttpSyncWorker so screenshot retries don't block event sync.
/// </summary>
public sealed class ScreenshotSyncWorker : BackgroundService
{
    private readonly EventStore      _store;
    private readonly ServerDiscovery _discovery;
    private readonly AgentSettings   _settings;
    private readonly ILogger<ScreenshotSyncWorker> _logger;

    private const int BatchSize = 10;

    public ScreenshotSyncWorker(
        EventStore store,
        ServerDiscovery discovery,
        IOptions<AgentSettings> options,
        ILogger<ScreenshotSyncWorker> logger)
    {
        _store     = store;
        _discovery = discovery;
        _settings  = options.Value;
        _logger    = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(30));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await SyncAsync(ct); }
            catch (Exception ex) { _logger.LogWarning("ScreenshotSyncWorker: {Msg}", ex.Message); }
        }
    }

    private async Task SyncAsync(CancellationToken ct)
    {
        var url = await _discovery.GetServerUrlAsync(ct);
        if (url is null) return;

        var pending = _store.GetUnsentScreenshots(BatchSize);
        foreach (var item in pending)
            await UploadAsync(url, item, ct);
    }

    private async Task UploadAsync(string url, ScreenshotPending item, CancellationToken ct)
    {
        var parts = item.ScreenshotPath.Replace('\\', '/').Split('/');
        if (parts.Length < 2)
        {
            _store.MarkScreenshotSent(item.EventId, 2);
            return;
        }

        var date   = parts[0];
        var fileId = Path.GetFileNameWithoutExtension(parts[1]);
        var local  = Path.Combine(_settings.ExpandedScreenshotDir, item.ScreenshotPath);

        if (!File.Exists(local))
        {
            _store.MarkScreenshotSent(item.EventId, 2);
            return;
        }

        try
        {
            using var stream  = File.OpenRead(local);
            using var content = new StreamContent(stream);
            content.Headers.ContentType = new("image/webp");

            var endpoint = $"{url}/api/v1/screenshots/{_settings.MachineId}/{date}/{fileId}";
            using var req = new HttpRequestMessage(HttpMethod.Put, endpoint);
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = content;

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            _store.MarkScreenshotSent(item.EventId, resp.IsSuccessStatusCode ? 1 : 2);

            if (!resp.IsSuccessStatusCode)
                _logger.LogDebug("Screenshot {Id} rejected: {Status}", item.EventId, (int)resp.StatusCode);
        }
        catch (Exception ex)
        {
            _store.MarkScreenshotSent(item.EventId, 2);
            _logger.LogDebug("Screenshot {Id} upload failed: {Msg}", item.EventId, ex.Message);
        }
    }
}
