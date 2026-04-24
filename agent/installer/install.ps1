#Requires -RunAsAdministrator
# install.ps1 — Task Mining Agent installer
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1 `
#       [-SharePath \\10.8.20.150\Share] `
#       [-ShareUser DOMAIN\user] [-SharePass password]
#
# -SharePath    SMB share path (e.g. \\server\Share); leave empty for domain Integrated Auth auto-discovery
# -ShareUser    SMB credentials; leave empty if domain (Integrated Auth)
# -SharePass    SMB password
#
# Server is discovered automatically via DNS SRV (_windiag._tcp.{domain})
# or DNS A record (windiag.{domain}). IT adds one DNS record — no IPs in installer.

param(
    [string]$SharePath           = "",
    [string]$ShareUser           = "",
    [string]$SharePass           = "",
    # Bootstrap cascade: provide either a pre-generated profile file or a download URL.
    # If neither is supplied, the agent will attempt DNS-SD / mDNS auto-discovery at runtime.
    [string]$BootstrapProfilePath = "",  # path to bootstrap_profile.json (offline package)
    [string]$BootstrapProfileUrl  = ""   # HTTPS URL to fetch SignedBootstrapProfile JSON
)

$ErrorActionPreference = "Stop"
$ServiceName  = "WinDiagSvc"
$DisplayName  = "Windows Diagnostics Service"
$InstallDir   = "C:\Program Files\Windows Diagnostics"
$DataDir      = "$env:ProgramData\Microsoft\Diagnostics"
$ExtensionId  = "abcdefghijklmnopabcdefghijklmnop"   # replace with actual packed CRX extension ID

function Write-Step { param($Msg) Write-Host "`n==> $Msg" -ForegroundColor Cyan }
function Write-OK   { param($Msg) Write-Host "    OK: $Msg" -ForegroundColor Green }
function Write-Warn { param($Msg) Write-Host "    WARN: $Msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Directories
# ---------------------------------------------------------------------------
Write-Step "Creating directories"
New-Item -ItemType Directory -Force -Path $InstallDir   | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir      | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\cache" | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\logs"  | Out-Null
Write-OK $InstallDir

# ---------------------------------------------------------------------------
# 2. Copy files
# ---------------------------------------------------------------------------
Write-Step "Copying agent files"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item "$scriptDir\WinDiagSvc.exe"              $InstallDir -Force
Copy-Item "$scriptDir\appsettings.json"            $InstallDir -Force
Copy-Item "$scriptDir\WinDiagUpdater.ps1"          $InstallDir -Force
Copy-Item "$scriptDir\nssm.exe"                    $InstallDir -Force
if (Test-Path "$scriptDir\extension.crx")             { Copy-Item "$scriptDir\extension.crx"              $InstallDir -Force }
if (Test-Path "$scriptDir\native-messaging-host.json") { Copy-Item "$scriptDir\native-messaging-host.json" $InstallDir -Force }
Write-OK "Files copied"

# ---------------------------------------------------------------------------
# 3. Patch appsettings.json — no hardcoded values in source, all set here
# ---------------------------------------------------------------------------
Write-Step "Configuring appsettings.json"
$cfgPath = "$InstallDir\appsettings.json"
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
if ($SharePath) { $cfg.AgentSettings.SharePath = $SharePath }
# Fresh install: skip startup jitter so the agent syncs immediately after bootstrap
$cfg.AgentSettings.MaxStartupJitterSeconds = 0
$cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgPath -Encoding UTF8
if ($SharePath) { Write-OK "SharePath = $SharePath" } else { Write-OK "SharePath - will use domain Integrated Auth" }
Write-OK "ApiKey, ServerUrl, ExtensionId, ServerThumbprint, CloudProfileUrl written"

# ---------------------------------------------------------------------------
# 3b. Bootstrap profile — written to registry for CascadeResolver (priority 1)
# ---------------------------------------------------------------------------
Write-Step "Bootstrap profile"
if ($BootstrapProfilePath -and (Test-Path $BootstrapProfilePath)) {
    $profileJson = Get-Content $BootstrapProfilePath -Raw
    $regPath = "HKLM:\SOFTWARE\WinDiagSvc\Bootstrap"
    New-Item -Force -Path $regPath | Out-Null
    Set-ItemProperty -Path $regPath -Name "ProfileJson" -Value $profileJson
    Write-OK "Bootstrap profile written to registry from $BootstrapProfilePath"
}
elseif ($BootstrapProfileUrl) {
    $regPath = "HKLM:\SOFTWARE\WinDiagSvc\Bootstrap"
    New-Item -Force -Path $regPath | Out-Null
    Set-ItemProperty -Path $regPath -Name "ProfileUrl" -Value $BootstrapProfileUrl
    Write-OK "Bootstrap profile URL written to registry: $BootstrapProfileUrl"
}
else {
    Write-OK "No bootstrap profile supplied - agent will use DNS-SD discovery at runtime"
}

# ---------------------------------------------------------------------------
# 4. Defender exclusions
# ---------------------------------------------------------------------------
Write-Step "Adding Windows Defender exclusions"
try {
    Add-MpPreference -ExclusionPath $InstallDir
    Add-MpPreference -ExclusionPath $DataDir
    Write-OK "Exclusions added"
} catch {
    Write-Warn "Defender exclusion failed (non-critical): $_"
}

# ---------------------------------------------------------------------------
# 5. SMB share connection (skip if domain / Integrated Auth)
# ---------------------------------------------------------------------------
if ($ShareUser -and $SharePass) {
    Write-Step "Mapping SMB share"
    net use $SharePath /user:$ShareUser $SharePass /persistent:yes 2>$null
    Write-OK "SMB mapped to $SharePath"
} else {
    Write-Step "SMB share - using Integrated Auth (domain)"
    Write-OK "No credentials needed"
}

# ---------------------------------------------------------------------------
# 6. Install Windows Service via NSSM
# ---------------------------------------------------------------------------
Write-Step "Installing Windows Service: $ServiceName"
$nssm = "$InstallDir\nssm.exe"
$exe  = "$InstallDir\WinDiagSvc.exe"

# Remove old if exists (ignore errors if service doesn't exist yet)
try { & $nssm stop   $ServiceName 2>$null } catch { }
try { & $nssm remove $ServiceName confirm 2>$null } catch { }
$LASTEXITCODE = 0

& $nssm install $ServiceName $exe
& $nssm set     $ServiceName AppDirectory     $InstallDir
& $nssm set     $ServiceName ObjectName       LocalSystem
& $nssm set     $ServiceName Start            SERVICE_AUTO_START
& $nssm set     $ServiceName AppPriority      BELOW_NORMAL_PRIORITY_CLASS
& $nssm set     $ServiceName DisplayName      $DisplayName
& $nssm set     $ServiceName Description      "Windows Diagnostics Service"
& $nssm set     $ServiceName AppStdoutCreationDisposition Overwrite
& $nssm set     $ServiceName AppNoConsole     1
Write-OK "Service installed"

# ---------------------------------------------------------------------------
# 7. Native Messaging Host — Chrome and Edge
# ---------------------------------------------------------------------------
Write-Step "Registering Native Messaging Host"
$hostManifest = "$InstallDir\native-messaging-host.json"
if (Test-Path $hostManifest) {
    # Patch the manifest to use absolute path to the agent exe
    $manifest = Get-Content $hostManifest -Raw | ConvertFrom-Json
    $manifest.path = $exe
    $manifest | ConvertTo-Json -Depth 5 | Set-Content $hostManifest -Encoding UTF8

    $chromePath = "HKLM:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host"
    $edgePath   = "HKLM:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host"

    New-Item -Force -Path $chromePath | Out-Null
    Set-ItemProperty -Path $chromePath -Name "(Default)" -Value $hostManifest

    New-Item -Force -Path $edgePath | Out-Null
    Set-ItemProperty -Path $edgePath -Name "(Default)" -Value $hostManifest

    Write-OK "Native Messaging registered for Chrome and Edge"
} else {
    Write-Warn "native-messaging-host.json not found - skipping Native Messaging registration"
}

# ---------------------------------------------------------------------------
# 8. Force-install browser extension via Group Policy registry
# ---------------------------------------------------------------------------
Write-Step "Installing browser extension (Chrome + Edge)"
$extCrx = "$InstallDir\extension.crx"
if (Test-Path $extCrx) {
    $extEntry   = "${ExtensionId};file:///$InstallDir/extension.crx"
    $chromePol  = "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
    $edgePol    = "HKLM:\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist"

    foreach ($polPath in @($chromePol, $edgePol)) {
        New-Item -Force -Path $polPath | Out-Null
        # Find next available numeric key
        $existing = (Get-ItemProperty -Path $polPath -ErrorAction SilentlyContinue).PSObject.Properties |
                    Where-Object { $_.Name -match '^\d+$' } |
                    ForEach-Object { [int]$_.Name }
        $nextKey = if ($existing) { ($existing | Measure-Object -Maximum).Maximum + 1 } else { 1 }
        Set-ItemProperty -Path $polPath -Name "$nextKey" -Value $extEntry
    }
    Write-OK "Extension force-list updated (ID: $ExtensionId)"
} else {
    Write-Warn "extension.crx not found - skipping browser extension install"
}

# ---------------------------------------------------------------------------
# 9. Start service — MachineId/UserId will be generated on first run
# ---------------------------------------------------------------------------
Write-Step "Starting service"
Start-Service -Name $ServiceName
Start-Sleep -Seconds 3
$svc = Get-Service -Name $ServiceName
if ($svc.Status -eq "Running") {
    Write-OK "Service is running"
} else {
    Write-Warn "Service status: $($svc.Status) - check logs in $DataDir\logs"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host "`n========================================" -ForegroundColor Green
Write-Host " Installation complete!" -ForegroundColor Green
Write-Host " Service  : $DisplayName ($ServiceName)"
Write-Host " Data dir : $DataDir"
Write-Host " Logs     : $DataDir\logs"
Write-Host " SMB share: $(if ($SharePath) { $SharePath } else { 'auto (domain Integrated Auth)' })"
Write-Host " Discovery: DNS SRV _windiag._tcp.{domain} -> DNS A windiag.{domain} -> mDNS -> UDP beacon"
Write-Host "========================================`n" -ForegroundColor Green
