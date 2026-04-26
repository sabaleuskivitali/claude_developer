using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture.Meeting;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Sync;

/// <summary>
/// Every 60 seconds: uploads pending meeting metadata + audio files to the server.
/// sent columns: 0=pending, 1=sent, 2=failed (retry next cycle).
/// </summary>
public sealed class MeetingUploaderWorker : BackgroundService
{
    private static readonly JsonSerializerOptions _json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private readonly EventStore      _store;
    private readonly ServerDiscovery _discovery;
    private readonly AgentSettings   _settings;
    private readonly ILogger<MeetingUploaderWorker> _logger;

    public MeetingUploaderWorker(
        EventStore store,
        ServerDiscovery discovery,
        IOptions<AgentSettings> options,
        ILogger<MeetingUploaderWorker> logger)
    {
        _store     = store;
        _discovery = discovery;
        _settings  = options.Value;
        _logger    = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(60));
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await UploadPendingAsync(ct); }
            catch (Exception ex) { _logger.LogWarning("MeetingUploader: {Msg}", ex.Message); }
        }
    }

    private async Task UploadPendingAsync(CancellationToken ct)
    {
        var url = await _discovery.GetServerUrlAsync(ct);
        if (url is null) return;

        var meetings = _store.GetPendingMeetings(20);
        foreach (var m in meetings)
        {
            await UploadMeetingAsync(url, m, ct);
        }
    }

    private async Task UploadMeetingAsync(string baseUrl, MeetingRecord m, CancellationToken ct)
    {
        // 1. POST meta
        if (m.MetaSent != 1)
        {
            var ok = await PostMetaAsync(baseUrl, m, ct);
            _store.SetMeetingMetaSent(m.MeetingId, ok ? 1 : 2);
        }

        // 2. POST mic audio
        if (m.MicSent != 1 && !string.IsNullOrEmpty(m.MicPath) && File.Exists(m.MicPath))
        {
            var ok = await PostAudioAsync(baseUrl, m.MachineId, m.MeetingId, "mic", m.MicPath, ct);
            _store.SetMeetingMicSent(m.MeetingId, ok ? 1 : 2);
        }
        else if (m.MicSent != 1)
        {
            _store.SetMeetingMicSent(m.MeetingId, 1); // no file — mark done
        }

        // 3. POST loopback audio
        if (m.LoopbackSent != 1 && !string.IsNullOrEmpty(m.LoopbackPath) && File.Exists(m.LoopbackPath))
        {
            var ok = await PostAudioAsync(baseUrl, m.MachineId, m.MeetingId, "loopback", m.LoopbackPath, ct);
            _store.SetMeetingLoopbackSent(m.MeetingId, ok ? 1 : 2);
        }
        else if (m.LoopbackSent != 1)
        {
            _store.SetMeetingLoopbackSent(m.MeetingId, 1); // no file — mark done
        }
    }

    private async Task<bool> PostMetaAsync(string baseUrl, MeetingRecord m, CancellationToken ct)
    {
        try
        {
            var payload = new
            {
                meeting_id   = m.MeetingId,
                machine_id   = m.MachineId,
                user_id      = m.UserId,
                started_at   = m.StartedAt,
                ended_at     = m.EndedAt,
                process_name = m.ProcessName,
                window_title = m.WindowTitle,
                trigger      = m.Trigger,
            };

            var endpoint = $"{baseUrl}/api/v1/meetings/{m.MachineId}/{m.MeetingId}/meta";
            using var req = new HttpRequestMessage(HttpMethod.Post, endpoint);
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = new StringContent(
                JsonSerializer.Serialize(payload, _json),
                System.Text.Encoding.UTF8,
                "application/json");

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            _logger.LogDebug("Meeting meta upload failed {Id}: {Msg}", m.MeetingId, ex.Message);
            return false;
        }
    }

    private async Task<bool> PostAudioAsync(
        string baseUrl, string machineId, string meetingId,
        string channel, string filePath, CancellationToken ct)
    {
        try
        {
            await using var stream  = File.OpenRead(filePath);
            using var content = new StreamContent(stream);
            content.Headers.ContentType = new MediaTypeHeaderValue("audio/ogg");

            var endpoint = $"{baseUrl}/api/v1/meetings/{machineId}/{meetingId}/audio/{channel}";
            using var req = new HttpRequestMessage(HttpMethod.Post, endpoint);
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = content;

            using var resp = await _discovery.HttpClient.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            _logger.LogDebug("Meeting audio upload failed {Id}/{Ch}: {Msg}", meetingId, channel, ex.Message);
            return false;
        }
    }
}
