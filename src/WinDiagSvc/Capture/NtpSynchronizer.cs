using GuerrillaNtp;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;

namespace WinDiagSvc.Capture;

/// <summary>
/// Polls NTP every NtpIntervalMinutes, tracks drift and drift rate.
/// SyncedTs() returns interpolated corrected timestamp from raw local ms.
/// Thread-safe: all mutable state under _lock.
/// </summary>
public sealed class NtpSynchronizer : BackgroundService
{
    private readonly AgentSettings _settings;
    private readonly ILogger<NtpSynchronizer> _logger;

    private readonly Lock _lock = new();
    private long   _lastDriftMs;
    private double _driftRatePpm;
    private long   _lastNtpLocalMs;   // local clock at last successful sync
    private string _serverUsed  = "";
    private long   _lastRoundTripMs;

    public long   CurrentDriftMs   { get { lock (_lock) return _lastDriftMs; } }
    public double DriftRatePpm     { get { lock (_lock) return _driftRatePpm; } }
    public string NtpServerUsed    { get { lock (_lock) return _serverUsed; } }
    public long   LastRoundTripMs  { get { lock (_lock) return _lastRoundTripMs; } }

    public NtpSynchronizer(IOptions<AgentSettings> options, ILogger<NtpSynchronizer> logger)
    {
        _settings = options.Value;
        _logger   = logger;
    }

    /// <summary>
    /// Returns corrected UTC timestamp in ms, interpolated from last NTP sync.
    /// </summary>
    public long SyncedTs(long rawUtcMs)
    {
        lock (_lock)
        {
            if (_lastNtpLocalMs == 0) return rawUtcMs;

            var elapsedMs = rawUtcMs - _lastNtpLocalMs;
            var correction = _lastDriftMs + (long)(_driftRatePpm * elapsedMs / 1_000_000.0);
            return rawUtcMs + correction;
        }
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // First sync immediately, then on interval
        await SyncOnceAsync();

        var interval = TimeSpan.FromMinutes(_settings.NtpIntervalMinutes);
        using var timer = new PeriodicTimer(interval);

        while (await timer.WaitForNextTickAsync(ct))
            await SyncOnceAsync();
    }

    private async Task SyncOnceAsync()
    {
        foreach (var server in _settings.NtpServers)
        {
            try
            {
                var result = await QueryMedianAsync(server);
                if (result is null) continue;

                var (driftMs, roundTripMs) = result.Value;

                if (roundTripMs > 200)
                {
                    _logger.LogDebug("NTP {Server}: round-trip {Rtt}ms too noisy, skipping", server, roundTripMs);
                    continue;
                }

                lock (_lock)
                {
                    var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

                    if (_lastNtpLocalMs > 0)
                    {
                        var elapsedMs = nowMs - _lastNtpLocalMs;
                        if (elapsedMs > 0)
                        {
                            var driftDelta = driftMs - _lastDriftMs;
                            _driftRatePpm = driftDelta * 1_000_000.0 / elapsedMs;
                        }
                    }

                    _lastDriftMs    = driftMs;
                    _lastNtpLocalMs = nowMs;
                    _lastRoundTripMs = roundTripMs;
                    _serverUsed     = server;
                }

                _logger.LogDebug("NTP sync OK: {Server}, drift={Drift}ms, rate={Rate:F1}ppm, rtt={Rtt}ms",
                    server, driftMs, _driftRatePpm, roundTripMs);
                return;
            }
            catch (Exception ex)
            {
                _logger.LogWarning("NTP {Server} failed: {Msg}", server, ex.Message);
            }
        }

        _logger.LogWarning("All NTP servers failed — using local clock");
    }

    private static async Task<(long driftMs, long roundTripMs)?> QueryMedianAsync(string server)
    {
        const int Samples = 5;
        var drifts    = new long[Samples];
        var roundTrips = new long[Samples];

        using var client = new NtpClient(server);

        for (var i = 0; i < Samples; i++)
        {
            var clock = await client.QueryAsync();
            drifts[i]     = (long)clock.CorrectionOffset.TotalMilliseconds;
            roundTrips[i] = (long)clock.RoundTripTime.TotalMilliseconds;
            if (i < Samples - 1)
                await Task.Delay(200);
        }

        Array.Sort(drifts);
        Array.Sort(roundTrips);

        return (drifts[Samples / 2], roundTrips[Samples / 2]);
    }
}
