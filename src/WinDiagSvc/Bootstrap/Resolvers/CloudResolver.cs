using System.Net.Http;
using System.Text.Json;

namespace WinDiagSvc.Bootstrap.Resolvers;

/// <summary>
/// Downloads the bootstrap profile from a URL supplied via:
///   1. Registry: HKLM\SOFTWARE\WinDiagSvc\Bootstrap\ProfileUrl
///   2. Environment variable: WINDIAG_BOOTSTRAP_URL
///
/// Used for cloud-assisted bootstrap or when IT sets a static URL.
/// The URL must serve a SignedBootstrapProfile JSON.
/// </summary>
public sealed class CloudResolver : IProfileResolver
{
    private const string RegKey      = @"SOFTWARE\WinDiagSvc\Bootstrap";
    private const string RegUrlValue = "ProfileUrl";
    private const string EnvVar      = "WINDIAG_BOOTSTRAP_URL";

    private static readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };

    public string Name => "cloud";

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

    private static string? GetUrl()
    {
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
