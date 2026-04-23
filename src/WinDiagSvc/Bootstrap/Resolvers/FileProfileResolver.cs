using System.Text.Json;

namespace WinDiagSvc.Bootstrap.Resolvers;

/// <summary>
/// Reads the bootstrap profile from bootstrap_profile.json next to the executable.
/// Written by the offline installer package.
/// </summary>
public sealed class FileProfileResolver : IProfileResolver
{
    private readonly string _path;

    public FileProfileResolver(string? path = null)
    {
        _path = path ?? Path.Combine(AppContext.BaseDirectory, "bootstrap_profile.json");
    }

    public string Name => "file";

    public Task<SignedBootstrapProfile?> TryResolveAsync(CancellationToken ct)
    {
        try
        {
            if (!File.Exists(_path))
                return Task.FromResult<SignedBootstrapProfile?>(null);

            var json    = File.ReadAllText(_path);
            var profile = JsonSerializer.Deserialize<SignedBootstrapProfile>(json);
            return Task.FromResult(profile);
        }
        catch
        {
            return Task.FromResult<SignedBootstrapProfile?>(null);
        }
    }
}
