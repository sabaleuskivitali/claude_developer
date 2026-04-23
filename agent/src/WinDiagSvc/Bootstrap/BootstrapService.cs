using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;

namespace WinDiagSvc.Bootstrap;

/// <summary>
/// Runs the cascade resolver once at startup and applies the bootstrap profile
/// to AgentSettings before any other service connects to the server.
///
/// Called synchronously from Program.cs via RunBootstrapAsync() before host.RunAsync().
/// </summary>
public sealed class BootstrapService
{
    private readonly CascadeResolver _resolver;
    private readonly EnrollmentClient _enrollment;
    private readonly IOptions<AgentSettings> _settingsOpts;
    private readonly ILogger<BootstrapService> _log;

    public BootstrapService(
        CascadeResolver resolver,
        EnrollmentClient enrollment,
        IOptions<AgentSettings> settingsOpts,
        ILogger<BootstrapService> log)
    {
        _resolver     = resolver;
        _enrollment   = enrollment;
        _settingsOpts = settingsOpts;
        _log          = log;
    }

    public async Task RunBootstrapAsync(CancellationToken ct = default)
    {
        var settings = _settingsOpts.Value;

        // If ServerUrl is already configured (legacy / manual), skip bootstrap.
        if (!string.IsNullOrEmpty(settings.ServerUrl))
        {
            _log.LogDebug("Bootstrap: ServerUrl already set — skipping cascade resolver");
            return;
        }

        var (signed, profile, method) = await _resolver.ResolveAsync(ct);
        if (profile is null)
        {
            _log.LogWarning("Bootstrap: no profile resolved — agent will run without server connection");
            return;
        }

        // Apply endpoints and trust from profile
        settings.ServerUrl = profile.Endpoints.Primary;

        if (profile.Trust.Pins.Length > 0)
            settings.ServerThumbprint = profile.Trust.Pins[0].Replace("sha256/", "");

        _log.LogInformation(
            "Bootstrap: applied profile {Id} (method={Method}, server={Url})",
            profile.ProfileId, method, settings.ServerUrl);

        // Enroll to obtain API key (MVP: token → API key)
        if (!string.IsNullOrEmpty(profile.Enrollment.Token) && string.IsNullOrEmpty(settings.ApiKey))
        {
            var apiKey = await _enrollment.EnrollAsync(profile, settings.MachineId, method ?? "unknown", ct);
            if (!string.IsNullOrEmpty(apiKey))
            {
                settings.ApiKey = apiKey;
                PersistSettings(settings);
            }
        }
    }

    private static void PersistSettings(AgentSettings settings)
    {
        try
        {
            var configPath = Path.Combine(AppContext.BaseDirectory, "appsettings.json");
            if (!File.Exists(configPath)) return;

            var json = File.ReadAllText(configPath);
            using var doc    = System.Text.Json.JsonDocument.Parse(json);
            using var ms     = new System.IO.MemoryStream();
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
                        if (p.Name == "ServerUrl" && string.IsNullOrEmpty(p.Value.GetString()))
                            writer.WriteString("ServerUrl", settings.ServerUrl);
                        else if (p.Name == "ApiKey" && string.IsNullOrEmpty(p.Value.GetString()))
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
