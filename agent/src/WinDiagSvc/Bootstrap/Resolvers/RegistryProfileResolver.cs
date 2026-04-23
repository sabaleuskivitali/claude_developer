using Microsoft.Win32;
using System.Text.Json;

namespace WinDiagSvc.Bootstrap.Resolvers;

/// <summary>
/// Reads the bootstrap profile from the Windows registry.
/// Written by GPO/Intune or by install.ps1 when -BootstrapProfilePath is supplied.
/// Key: HKLM\SOFTWARE\WinDiagSvc\Bootstrap, value: ProfileJson (REG_SZ)
/// </summary>
public sealed class RegistryProfileResolver : IProfileResolver
{
    private const string RegKey   = @"SOFTWARE\WinDiagSvc\Bootstrap";
    private const string RegValue = "ProfileJson";

    public string Name => "registry";

    public Task<SignedBootstrapProfile?> TryResolveAsync(CancellationToken ct)
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(RegKey);
            if (key?.GetValue(RegValue) is not string json || string.IsNullOrWhiteSpace(json))
                return Task.FromResult<SignedBootstrapProfile?>(null);

            var profile = JsonSerializer.Deserialize<SignedBootstrapProfile>(json);
            return Task.FromResult(profile);
        }
        catch
        {
            return Task.FromResult<SignedBootstrapProfile?>(null);
        }
    }
}
