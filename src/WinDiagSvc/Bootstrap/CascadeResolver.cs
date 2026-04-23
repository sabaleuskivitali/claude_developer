using Microsoft.Extensions.Logging;
using WinDiagSvc.Bootstrap.Resolvers;

namespace WinDiagSvc.Bootstrap;

/// <summary>
/// Tries resolvers in priority order, verifies signature, returns first valid profile.
///
/// Priority:
///   1. Registry   — GPO/Intune/install.ps1
///   2. File       — offline package (bootstrap_profile.json beside exe)
///   3. Cloud      — WINDIAG_BOOTSTRAP_URL env / registry ProfileUrl
///   4. DNS-SD     — mDNS discovery (L2 only)
/// </summary>
public sealed class CascadeResolver
{
    private readonly IReadOnlyList<IProfileResolver> _resolvers;
    private readonly ILogger<CascadeResolver> _log;

    public CascadeResolver(ILogger<CascadeResolver> log)
    {
        _log = log;
        _resolvers =
        [
            new RegistryProfileResolver(),
            new FileProfileResolver(),
            new CloudResolver(),
            new DnsSdResolver(),
        ];
    }

    public async Task<(SignedBootstrapProfile? signed, BootstrapProfile? profile, string? method)>
        ResolveAsync(CancellationToken ct = default)
    {
        foreach (var resolver in _resolvers)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                var signed = await resolver.TryResolveAsync(ct);
                if (signed is null) continue;

                if (!ProfileVerifier.Verify(signed))
                {
                    _log.LogWarning("Bootstrap [{Method}]: signature verification failed — skipping", resolver.Name);
                    continue;
                }

                var profile = signed.GetProfile();
                if (profile.IsExpired)
                {
                    _log.LogWarning("Bootstrap [{Method}]: profile expired ({At}) — skipping", resolver.Name, profile.ExpiresAt);
                    continue;
                }

                _log.LogInformation("Bootstrap: resolved via [{Method}] (profile {Id}, expires {At})",
                    resolver.Name, profile.ProfileId, profile.ExpiresAt);
                return (signed, profile, resolver.Name);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                _log.LogDebug(ex, "Bootstrap [{Method}]: resolver threw", resolver.Name);
            }
        }

        _log.LogWarning("Bootstrap: no valid profile found from any resolver");
        return (null, null, null);
    }
}
