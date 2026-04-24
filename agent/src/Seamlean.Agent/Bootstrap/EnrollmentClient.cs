using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace Seamlean.Agent.Bootstrap;

/// <summary>
/// Exchanges the enrollment token from a BootstrapProfile for agent credentials.
/// MVP: token is returned as ApiKey (simple bearer token auth).
/// Phase 4+: replace with mTLS CSR flow.
/// </summary>
public sealed class EnrollmentClient
{
    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private readonly ILogger<EnrollmentClient> _log;

    public EnrollmentClient(ILogger<EnrollmentClient> log) => _log = log;

    /// <summary>
    /// Enroll this machine. Returns the API key on success, null on failure.
    /// Idempotent: safe to call on every startup (server ignores already-used tokens for
    /// machines that already have a valid registration).
    /// </summary>
    public async Task<string?> EnrollAsync(
        BootstrapProfile profile,
        string machineId,
        string method,
        CancellationToken ct = default)
    {
        try
        {
            var payload = new
            {
                machine_id = machineId,
                token      = profile.Enrollment.Token,
                method,
            };

            var response = await _http.PostAsJsonAsync(
                profile.Enrollment.CsrEndpoint, payload, ct);

            if (!response.IsSuccessStatusCode)
            {
                var err = await response.Content.ReadAsStringAsync(ct);
                _log.LogWarning("Enrollment failed ({Status}): {Error}", (int)response.StatusCode, err);
                return null;
            }

            var json   = await response.Content.ReadAsStringAsync(ct);
            var doc    = JsonDocument.Parse(json);
            var apiKey = doc.RootElement.TryGetProperty("api_key", out var prop) ? prop.GetString() : null;

            if (!string.IsNullOrEmpty(apiKey))
                _log.LogInformation("Enrollment succeeded for machine {MachineId}", machineId);

            return apiKey;
        }
        catch (Exception ex)
        {
            _log.LogWarning(ex, "Enrollment request failed");
            return null;
        }
    }
}
