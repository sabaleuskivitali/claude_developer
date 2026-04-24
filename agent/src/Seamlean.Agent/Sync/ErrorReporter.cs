using System.Net.Http;
using System.Net.Http.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;

namespace Seamlean.Agent.Sync;

/// <summary>
/// Fire-and-forget error reporting to /api/v1/errors.
/// Never throws — loss of an error report is acceptable.
/// </summary>
public sealed class ErrorReporter
{
    private readonly ServerDiscovery _discovery;
    private readonly AgentSettings   _settings;
    private readonly ILogger<ErrorReporter> _logger;

    private static readonly Version _agentVersion =
        typeof(ErrorReporter).Assembly.GetName().Version ?? new Version(1, 0);

    public ErrorReporter(
        ServerDiscovery discovery,
        IOptions<AgentSettings> options,
        ILogger<ErrorReporter> logger)
    {
        _discovery = discovery;
        _settings  = options.Value;
        _logger    = logger;
    }

    public void Report(string stage, string error, object? payload = null) =>
        _ = ReportAsync(stage, error, payload);

    private async Task ReportAsync(string stage, string error, object? payload)
    {
        try
        {
            var url = await _discovery.GetServerUrlAsync();
            if (url is null) return;

            var body = new
            {
                machine_id    = _settings.MachineId,
                stage,
                error         = error[..Math.Min(error.Length, 2000)],
                os_version    = Environment.OSVersion.VersionString,
                agent_version = _agentVersion.ToString(),
                ts            = DateTimeOffset.UtcNow.ToString("O"),
                payload,
            };

            using var req = new HttpRequestMessage(HttpMethod.Post, $"{url}/api/v1/errors");
            req.Headers.Add("X-Api-Key", _settings.ApiKey);
            req.Content = JsonContent.Create(body);

            using var resp = await _discovery.HttpClient.SendAsync(req);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("ErrorReporter: {Msg}", ex.Message);
        }
    }
}
