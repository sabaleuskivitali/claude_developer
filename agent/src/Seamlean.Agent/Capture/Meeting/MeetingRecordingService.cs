using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;

namespace Seamlean.Agent.Capture.Meeting;

/// <summary>
/// Coordinator: starts/stops parallel MicRecorder + LoopbackRecorder,
/// persists the resulting MeetingRecord in SQLite.
/// Thread-safe: concurrent calls to StartRecording are idempotent.
/// </summary>
public sealed class MeetingRecordingService
{
    private const int MaxMeetingHours = 4;

    private readonly EventStore  _store;
    private readonly AgentSettings _settings;
    private readonly ILogger<MeetingRecordingService> _logger;

    private readonly SemaphoreSlim _lock = new(1, 1);

    private MicRecorder?      _mic;
    private LoopbackRecorder? _loopback;
    private MeetingRecord?    _current;
    private CancellationTokenSource? _safetyCts;

    public bool IsRecording => _current is not null;

    public MeetingRecordingService(
        EventStore store,
        IOptions<AgentSettings> options,
        ILogger<MeetingRecordingService> logger)
    {
        _store    = store;
        _settings = options.Value;
        _logger   = logger;
    }

    public async Task StartRecordingAsync(string trigger, string? processName, string? windowTitle)
    {
        if (!await _lock.WaitAsync(0)) return; // already locked — ignore concurrent call
        try
        {
            if (_current is not null) return; // already recording

            var meetingId = Guid.NewGuid().ToString("N");
            var now       = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var dir       = Path.Combine(MeetingsDir, DateTime.UtcNow.ToString("yyyyMMdd"), meetingId);

            var record = new MeetingRecord
            {
                MeetingId   = meetingId,
                MachineId   = _settings.MachineId,
                UserId      = _settings.UserId,
                StartedAt   = now,
                Trigger     = trigger,
                ProcessName = processName,
                WindowTitle = windowTitle,
            };

            _store.InsertMeeting(record);

            _mic      = new MicRecorder();
            _loopback = new LoopbackRecorder();

            var micPath      = Path.Combine(dir, "mic.ogg");
            var loopbackPath = Path.Combine(dir, "loopback.ogg");

            try { await _mic.StartAsync(micPath); }
            catch (Exception ex) { _logger.LogWarning("MicRecorder start failed: {Msg}", ex.Message); await _mic.DisposeAsync(); _mic = null; }

            try { await _loopback.StartAsync(loopbackPath); }
            catch (Exception ex) { _logger.LogWarning("LoopbackRecorder start failed: {Msg}", ex.Message); await _loopback.DisposeAsync(); _loopback = null; }

            _current   = record;
            _safetyCts = new CancellationTokenSource();

            // Safety cutoff: stop after MaxMeetingHours regardless
            _ = Task.Delay(TimeSpan.FromHours(MaxMeetingHours), _safetyCts.Token)
                    .ContinueWith(async t =>
                    {
                        if (!t.IsCanceled)
                            await StopRecordingAsync("safety_cutoff");
                    });

            _logger.LogInformation("Meeting recording started: {Id} trigger={T}", meetingId, trigger);
        }
        finally { _lock.Release(); }
    }

    public async Task StopRecordingAsync(string reason = "manual")
    {
        await _lock.WaitAsync();
        try
        {
            if (_current is null) return;

            _safetyCts?.Cancel();
            _safetyCts?.Dispose();
            _safetyCts = null;

            var micPath      = _mic      is not null ? await _mic.StopAsync()      : null;
            var loopbackPath = _loopback is not null ? await _loopback.StopAsync() : null;

            if (_mic is not null)      await _mic.DisposeAsync();
            if (_loopback is not null) await _loopback.DisposeAsync();
            _mic      = null;
            _loopback = null;

            var endedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            _store.UpdateMeetingEnded(_current.MeetingId, endedAt, micPath, loopbackPath);

            _logger.LogInformation("Meeting recording stopped: {Id} reason={R} mic={M} loopback={L}",
                _current.MeetingId, reason, micPath is not null, loopbackPath is not null);

            _current = null;
        }
        finally { _lock.Release(); }
    }

    private string MeetingsDir =>
        Path.Combine(
            Path.GetDirectoryName(_settings.ExpandedScreenshotDir)!,
            "meetings");
}
