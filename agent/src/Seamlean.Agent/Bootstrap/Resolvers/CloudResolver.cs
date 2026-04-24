using System.Net.Http;
using System.Text.Json;

namespace Seamlean.Agent.Bootstrap.Resolvers;

/// <summary>
/// Downloads the bootstrap profile from a URL supplied via (priority order):
///   1. AgentSettings.CloudProfileUrl  (appsettings.json — injected at build time by CI)
///   2. Registry: HKLM\SOFTWARE\WinDiagSvc\Bootstrap\ProfileUrl
///   3. Environment variable: WINDIAG_BOOTSTRAP_URL
/// </summary>
public sealed class CloudResolver : IProfileResolver
{
    private const string RegKey      = @"SOFTWARE\WinDiagSvc\Bootstrap";
    private const string RegUrlValue = "ProfileUrl";
    private const string EnvVar      = "WINDIAG_BOOTSTRAP_URL";

    // Cloud server uses self-signed cert; security relies on ECDSA profile signature verification.
    private static readonly HttpClientHandler _handler = new()
    {
        ServerCertificateCustomValidationCallback = HttpClientHandler.DangerousAcceptAnyServerCertificateValidator
    };
    private static readonly HttpClient _http = new(_handler) { Timeout = TimeSpan.FromSeconds(10) };

    private readonly string _settingsUrl;
    public string Name => "cloud";

    public CloudResolver(string settingsUrl = "")
    {
        _settingsUrl = settingsUrl;
    }

    public async Task<SignedBootstrapProfile?> TryResolveAsync(CancellationToken ct)
    {
        var url = GetUrl();
        if (string.IsNullOrEmpty(url))
            return null;

        try
        {
            var json    = await _http.GetStringAsync(url, ct);
            var profile = JsonSerializer.Deserialize<SignedBootstrapProfile>(json);
            return profile;
        }
        catch
        {
            return null;
        }
    }

    private string? GetUrl()
    {
        if (!string.IsNullOrEmpty(_settingsUrl)) return _settingsUrl;

        var env = Environment.GetEnvironmentVariable(EnvVar);
        if (!string.IsNullOrEmpty(env)) return env;

        try
        {
            using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(RegKey);
            return key?.GetValue(RegUrlValue) as string;
        }
        catch
        {
            return null;
        }
    }
}
