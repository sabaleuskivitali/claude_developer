using System.Net.Http;
using System.Net.Http.Json;
using System.ServiceProcess;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Capture;
using Seamlean.Agent.Models;
using Seamlean.Agent.Storage;
using Seamlean.Agent.Sync;

namespace Seamlean.Agent.Management;

/// <summary>
/// Replaces CommandPoller. GET /api/v1/commands/{machine_id} every CommandPollIntervalSeconds.
/// Executes the command and POST /api/v1/commands/{machine_id}/ack.
/// </summary>
public sealed class HttpCommandPoller : BackgroundService
{
    private readonly EventStore      _store;
    private readonly NtpSynchronizer _ntp;
    private readonly ServerDiscovery _discovery;
    private readonly AgentSettings   _settings;
    private readonly LayerWatchdog   _watchdog;
    private readonly ILogger<HttpCommandPoller> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
        { PropertyNameCaseInsensitive = true };

    public HttpCommandPoller(
        EventStore store,
        NtpSynchronizer ntp,
        ServerDiscovery discovery,
        LayerWatchdog watchdog,
        IOptions<AgentSettings> options,
        ILogger<HttpCommandPoller> logger)
    {
        _store     = store;
        _ntp       = ntp;
        _discovery = discovery;
        _watchdog  = watchdog;
        _settings  = options.Value;
        _logger    = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var interval = TimeSpan.FromSeconds(_settings.CommandPollIntervalSeconds);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await CheckCommandAsync(ct); }
            catch (Exception ex) { _logger.LogDebug("HttpCommandPoller: {Msg}", ex.Message); }
        }
    }

    private async Task CheckCommandAsync(CancellationToken ct)
    {
        var url = await _discovery.GetServerUrlAsync(ct);
        if (url is null) return;

        using var req = new HttpRequestMessage(
            HttpMethod.Get, $"{url}/api/v1/commands/{_settings.MachineId}");
        req.Headers.Add("X-Api-Key", _settings.ApiKey);

        using var resp = await _discovery.HttpClient.SendAsync(req, ct);
        if (!resp.IsSuccessStatusCode) return;

        var body = await resp.Content.ReadAsStringAsync(ct);
        if (string.IsNullOrEmpty(body) || body == "null") return;

        var cmd = JsonSerializer.Deserialize<AgentCommand>(body, _jsonOpts);
        if (cmd is null) return;

        _logger.LogInformation("HttpCommandPoller: executing {Cmd}", cmd.Command);
        WriteEventReceived(cmd);

        var (status, message) = ExecuteCommand(cmd);
        await SendAckAsync(url, cmd, status, message, ct);
        WriteEventExecuted(cmd, status, message);
    }

    private async Task SendAckAsync(string url, AgentCommand cmd, string status, string message, CancellationToken ct)
    {
        try
        {
            var ack = new
            {
                command_id      = cmd.CommandId,
                status,
                message,
                service_state   = "running",
                events_buffered = _store.CountPending(),
                drift_ms        = _ntp.CurrentDriftMs,
            };

            using var req = new HttpRequestMessage(
                HttpMethod.Post, $"{url}/api/v1/commands/{_settings.MachineId}/ack");
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = JsonContent.Create(ack);

            await _discovery.HttpClient.SendAsync(req, ct);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("Ack failed: {Msg}", ex.Message);
        }
    }

    private (string status, string message) ExecuteCommand(AgentCommand cmd)
    {
        try
        {
            return cmd.Command switch
            {
                "restart"       => RestartViaWatchdog("remote_command"),
                "restart_agent" => RestartViaWatchdog("remote_command"),
                "restart_layer" when cmd.Params != null
                    && cmd.Params.TryGetValue("layer", out var l)
                    => RestartLayerViaWatchdog(l?.ToString() ?? ""),
                "stop"    => StopService(),
                "start"   => StartService(),
                "status_dump" => (
                    "ok",
                    $"pending={_store.CountPending()}, failed={_store.CountFailed()}, " +
                    $"drift_ms={_ntp.CurrentDriftMs}, ntp={_ntp.NtpServerUsed}"),
                "update_config" when cmd.Params != null => ApplyConfigPatch(cmd.Params),
                _ => ("error", $"Unknown command: {cmd.Command}"),
            };
        }
        catch (Exception ex)
        {
            return ("error", ex.Message[..Math.Min(ex.Message.Length, 200)]);
        }
    }

    private (string, string) RestartViaWatchdog(string reason)
    {
        // 5 s delay: CheckCommandAsync calls SendAckAsync after this returns,
        // which is an async HTTP POST that may take up to ~3 s on a slow LAN.
        // Giving 5 s ensures the ack completes before Environment.Exit fires.
        Task.Delay(5_000).ContinueWith(_ => _watchdog.RestartAgent(reason));
        return ("ok", $"Agent restarting in 5s (trigger={reason})");
    }

    private (string, string) RestartLayerViaWatchdog(string layer)
    {
        if (string.IsNullOrWhiteSpace(layer))
            return ("error", "restart_layer requires params.layer");
        Task.Delay(5_000).ContinueWith(_ => _watchdog.RestartLayer(layer));
        return ("ok", $"Layer '{layer}' restart scheduled in 5s (full process restart)");
    }

    private static (string, string) RestartService()
    {
        using var sc = new ServiceController("WinDiagSvc");
        sc.Stop();
        sc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
        sc.Start();
        return ("ok", "Service restarted");
    }

    private static (string, string) StopService()
    {
        using var sc = new ServiceController("WinDiagSvc");
        sc.Stop();
        return ("ok", "Service stopped");
    }

    private static (string, string) StartService()
    {
        using var sc = new ServiceController("WinDiagSvc");
        sc.Start();
        return ("ok", "Service started");
    }

    private static (string, string) ApplyConfigPatch(Dictionary<string, object> patch)
    {
        var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
        if (!File.Exists(configPath)) return ("error", "appsettings.json not found");

        var json = File.ReadAllText(configPath);
        foreach (var kv in patch)
            json = System.Text.RegularExpressions.Regex.Replace(
                json,
                $"(\"{kv.Key}\"\\s*:\\s*)([^,\\n}}]+)",
                $"${{1}}{JsonSerializer.Serialize(kv.Value)}");

        File.WriteAllText(configPath, json);
        return ("ok", "Config updated — restart required");
    }

    private void WriteEventReceived(AgentCommand cmd)
    {
        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            SessionId = _store.SessionId, MachineId = _settings.MachineId,
            UserId = _settings.UserId, TimestampUtc = raw,
            SyncedTs = _ntp.SyncedTs(raw), DriftMs = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm, Layer = "agent",
            EventType = nameof(EventType.CommandReceived), RawMessage = cmd.Command,
        });
    }

    private void WriteEventExecuted(AgentCommand cmd, string status, string message)
    {
        var raw = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        _store.Insert(new ActivityEvent
        {
            SessionId = _store.SessionId, MachineId = _settings.MachineId,
            UserId = _settings.UserId, TimestampUtc = raw,
            SyncedTs = _ntp.SyncedTs(raw), DriftMs = _ntp.CurrentDriftMs,
            DriftRatePpm = _ntp.DriftRatePpm, Layer = "agent",
            EventType = nameof(EventType.CommandExecuted),
            RawMessage = $"{cmd.Command}: {status} — {message}",
        });
    }

    private sealed class AgentCommand
    {
        public string CommandId { get; set; } = "";
        public string Command   { get; set; } = "";
        public Dictionary<string, object>? Params { get; set; }
    }
}
