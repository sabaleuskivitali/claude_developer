using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Seamlean.Agent.Models;

namespace Seamlean.Agent.Bootstrap;

/// <summary>
/// Background service that monitors the active bootstrap profile and automatically
/// re-enrolls when the profile approaches expiry or is revoked.
/// Checks every 6 hours. Re-enrolls 7 days before expiry.
/// </summary>
public sealed class ReEnrollmentService : BackgroundService
{
    private static readonly TimeSpan CheckInterval  = TimeSpan.FromHours(6);
    private static readonly TimeSpan RenewThreshold = TimeSpan.FromDays(7);

    private readonly CascadeResolver _resolver;
    private readonly EnrollmentClient _enrollment;
    private readonly IOptions<AgentSettings> _settingsOpts;
    private readonly ILogger<ReEnrollmentService> _log;

    public ReEnrollmentService(
        CascadeResolver resolver,
        EnrollmentClient enrollment,
        IOptions<AgentSettings> settingsOpts,
        ILogger<ReEnrollmentService> log)
    {
        _resolver     = resolver;
        _enrollment   = enrollment;
        _settingsOpts = settingsOpts;
        _log          = log;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Initial delay — let other services start first
        await Task.Delay(TimeSpan.FromMinutes(2), ct);

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await CheckAndRenewAsync(ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                _log.LogError(ex, "Re-enrollment check failed");
            }

            await Task.Delay(CheckInterval, ct);
        }
    }

    private async Task CheckAndRenewAsync(CancellationToken ct)
    {
        var settings = _settingsOpts.Value;

        // Re-resolve the profile to detect expiry / revocation
        var (signed, profile, method) = await _resolver.ResolveAsync(ct);
        if (profile is null) return;

        if (!DateTime.TryParse(profile.ExpiresAt, out var expiresAt))
            return;

        var timeLeft = expiresAt.ToUniversalTime() - DateTime.UtcNow;
        if (timeLeft > RenewThreshold) return;

        _log.LogInformation(
            "Bootstrap profile expires in {Days:0} days — triggering re-enrollment",
            timeLeft.TotalDays);

        var apiKey = await _enrollment.EnrollAsync(profile, settings.MachineId, method ?? "re-enroll", ct);
        if (!string.IsNullOrEmpty(apiKey))
        {
            ApplyToSettings(settings, profile, apiKey);
            _log.LogInformation("Re-enrollment successful");
        }
    }

    private static void ApplyToSettings(AgentSettings settings, BootstrapProfile profile, string apiKey)
    {
        settings.ServerUrl = profile.Endpoints.Primary;
        settings.ApiKey    = apiKey;

        if (profile.Trust.Pins.Length > 0)
            settings.ServerThumbprint = profile.Trust.Pins[0].Replace("sha256/", "");

        PersistSettings(settings);
    }

    private static void PersistSettings(AgentSettings settings)
    {
        try
        {
            var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
            if (!File.Exists(configPath)) return;

            var json = File.ReadAllText(configPath);
            using var doc  = System.Text.Json.JsonDocument.Parse(json);
            using var ms   = new System.IO.MemoryStream();
            using var writer = new System.Text.Json.Utf8JsonWriter(ms, new System.Text.Json.JsonWriterOptions { Indented = true });
            writer.WriteStartObject();
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                if (prop.Name == "AgentSettings")
                {
                    writer.WritePropertyName("AgentSettings");
                    writer.WriteStartObject();
                    foreach (var p in prop.Value.EnumerateObject())
                    {
                        if (p.Name == "ServerUrl")
                            writer.WriteString("ServerUrl", settings.ServerUrl);
                        else if (p.Name == "ApiKey")
                            writer.WriteString("ApiKey", settings.ApiKey);
                        else
                            p.WriteTo(writer);
                    }
                    writer.WriteEndObject();
                }
                else
                {
                    prop.WriteTo(writer);
                }
            }
            writer.WriteEndObject();
            writer.Flush();
            File.WriteAllBytes(configPath, ms.ToArray());
        }
        catch
        {
            // Non-fatal: settings already applied in memory
        }
    }
}
