using System.ServiceProcess;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Capture;
using WinDiagSvc.Models;
using WinDiagSvc.Storage;

namespace WinDiagSvc.Management;

/// <summary>
/// Polls {SharePath}\{MachineId}\cmd\pending.json every CommandPollIntervalSeconds.
/// Executes the command and writes ack.json.
/// Commands older than 10 minutes are silently expired.
/// </summary>
public sealed class CommandPoller : BackgroundService
{
    private readonly EventStore _store;
    private readonly NtpSynchronizer _ntp;
    private readonly AgentSettings _settings;
    private readonly ILogger<CommandPoller> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
        { PropertyNameCaseInsensitive = true };

    public CommandPoller(
        EventStore store,
        NtpSynchronizer ntp,
        IOptions<AgentSettings> options,
        ILogger<CommandPoller> logger)
    {
        _store    = store;
        _ntp      = ntp;
        _settings = options.Value;
        _logger   = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var interval = TimeSpan.FromSeconds(_settings.CommandPollIntervalSeconds);
        using var timer = new PeriodicTimer(interval);
        while (await timer.WaitForNextTickAsync(ct))
        {
            try { await CheckCommandAsync(); }
            catch (Exception ex) { _logger.LogDebug("CommandPoller: {Msg}", ex.Message); }
        }
    }

    private async Task CheckCommandAsync()
    {
        var cmdPath = Path.Combine(_settings.SharePath, _settings.MachineId, "cmd", "pending.json");
        if (!File.Exists(cmdPath)) return;

        var json = await File.ReadAllTextAsync(cmdPath);
        var cmd  = JsonSerializer.Deserialize<AgentCommand>(json, _jsonOpts);
        if (cmd is null) return;

        // Expire stale commands
        if ((DateTimeOffset.UtcNow - cmd.IssuedAt).TotalMinutes > 10)
        {
            WriteAck(cmd, "expired", "Command expired");
            SafeDelete(cmdPath);
            return;
        }

        _logger.LogInformation("CommandPoller: executing {Cmd}", cmd.Command);

        WriteEventReceived(cmd);

        var (status, message) = ExecuteCommand(cmd);

        WriteAck(cmd, status, message);
        SafeDelete(cmdPath);
        WriteEventExecuted(cmd, status, message);
    }

    private (string status, string message) ExecuteCommand(AgentCommand cmd)
    {
        try
        {
            switch (cmd.Command)
            {
                case "restart":
                    using (var sc = new ServiceController("WinDiagSvc"))
                    {
                        sc.Stop();
                        sc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
                        sc.Start();
                    }
                    return ("ok", "Service restarted");

                case "stop":
                    using (var sc = new ServiceController("WinDiagSvc"))
                        sc.Stop();
                    return ("ok", "Service stopped");

                case "start":
                    using (var sc = new ServiceController("WinDiagSvc"))
                        sc.Start();
                    return ("ok", "Service started");

                case "status_dump":
                    var status = $"pending={_store.CountPending()}, failed={_store.CountFailed()}, " +
                                 $"drift_ms={_ntp.CurrentDriftMs}, ntp_server={_ntp.NtpServerUsed}";
                    return ("ok", status);

                case "update_config":
                    if (cmd.Params != null)
                        ApplyConfigPatch(cmd.Params);
                    return ("ok", "Config updated — restart required");

                default:
                    return ("error", $"Unknown command: {cmd.Command}");
            }
        }
        catch (Exception ex)
        {
            return ("error", ex.Message[..Math.Min(ex.Message.Length, 200)]);
        }
    }

    private static void ApplyConfigPatch(Dictionary<string, object> patch)
    {
        var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
        if (!File.Exists(configPath)) return;

        var json = File.ReadAllText(configPath);
        // Simple key-value patch inside AgentSettings section
        foreach (var kv in patch)
            json = System.Text.RegularExpressions.Regex.Replace(
                json,
                $"(\"{kv.Key}\"\\s*:\\s*)([^,\\n}}]+)",
                $"${{1}}{JsonSerializer.Serialize(kv.Value)}");

        File.WriteAllText(configPath, json);
    }

    private void WriteAck(AgentCommand cmd, string status, string message)
    {
        var ackPath = Path.Combine(_settings.SharePath, _settings.MachineId, "cmd", "ack.json");
        var ack = new
        {
            command_id    = cmd.CommandId,
            machine_id    = _settings.MachineId,
            executed_at   = DateTimeOffset.UtcNow,
            status,
            message,
            events_buffered = _store.CountPending(),
            drift_ms        = _ntp.CurrentDriftMs,
        };
        File.WriteAllText(ackPath, JsonSerializer.Serialize(ack));
    }

    private void WriteEventReceived(AgentCommand cmd)
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
            EventType    = nameof(EventType.CommandReceived),
            RawMessage   = cmd.Command,
        });
    }

    private void WriteEventExecuted(AgentCommand cmd, string status, string message)
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
            EventType    = nameof(EventType.CommandExecuted),
            RawMessage   = $"{cmd.Command}: {status} — {message}",
        });
    }

    private static void SafeDelete(string path)
    {
        try { File.Delete(path); } catch { }
    }

    // -----------------------------------------------------------------------
    // DTO
    // -----------------------------------------------------------------------

    private sealed class AgentCommand
    {
        public string CommandId { get; set; } = "";
        public string Command   { get; set; } = "";
        public DateTimeOffset IssuedAt { get; set; }
        public Dictionary<string, object>? Params { get; set; }
    }
}
