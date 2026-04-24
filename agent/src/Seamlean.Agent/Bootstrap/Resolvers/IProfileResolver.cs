namespace Seamlean.Agent.Bootstrap.Resolvers;

/// <summary>
/// Attempts to locate a signed bootstrap profile from one specific source.
/// Returns null if the source has nothing to offer (not an error — try next resolver).
/// </summary>
public interface IProfileResolver
{
    string Name { get; }
    Task<SignedBootstrapProfile?> TryResolveAsync(CancellationToken ct);
}
