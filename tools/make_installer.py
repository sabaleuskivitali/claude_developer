"""
make_installer.py — Bundle all binaries into a single self-contained PowerShell installer.

Usage:
    python tools/make_installer.py \
        --version  1.0.42 \
        --build    build \
        --out      build/Install-WinDiagSvc.ps1

Inputs (from build/ directory):
    WinDiagSvc.exe
    nssm.exe
    extension.crx
    extension-id.txt
    appsettings.json         (from src/)
    native-messaging-host.json (patched with real extension ID)
    WinDiagUpdater.ps1

Output:
    A single .ps1 that extracts everything and installs the agent.
    No other files needed on the target machine.
"""

import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


INSTALLER_TEMPLATE = r'''#Requires -RunAsAdministrator
# WinDiagSvc Installer v{VERSION}
# Generated: {GENERATED}
# Single-file: all binaries embedded as base64
#
# Usage:
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

function Write-Step {{ param($M) Write-Host "`n==> $M" -ForegroundColor Cyan }}
function Write-OK   {{ param($M) Write-Host "    OK: $M" -ForegroundColor Green }}
function Write-Warn {{ param($M) Write-Host "    WARN: $M" -ForegroundColor Yellow }}

# ---------------------------------------------------------------------------
# Extract embedded files to temp directory
# ---------------------------------------------------------------------------

$TempDir = Join-Path $env:TEMP "WinDiagInstall_$(Get-Random)"
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

Write-Step "Extracting embedded files"

function Expand-Embedded {{
    param([string]$Name, [string]$B64)
    $dest = Join-Path $TempDir $Name
    [IO.File]::WriteAllBytes($dest, [Convert]::FromBase64String($B64))
    return $dest
}}

{FILE_EXTRACTIONS}

Write-OK "Files extracted to $TempDir"

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
Copy-Item $AgentExe                $InstallDir -Force
Copy-Item $NssmExe                 $InstallDir -Force
Copy-Item $ExtensionCrx            $InstallDir -Force
Copy-Item $NativeHostJson          $InstallDir -Force
Copy-Item $AppSettingsJson         $InstallDir -Force
Copy-Item $UpdaterScript           $InstallDir -Force
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

& $nssm stop    $ServiceName 2>$null
& $nssm remove  $ServiceName confirm 2>$null

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

# Update path in manifest to point to installed exe
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
# Cleanup temp
# ---------------------------------------------------------------------------
Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " WinDiagSvc v$Version — installed!" -ForegroundColor Green
Write-Host " Service  : $DisplayName ($ServiceName)"
Write-Host " Data dir : $DataDir"
Write-Host " Logs     : $DataDir\logs"
Write-Host " SMB      : $SharePath"
Write-Host "========================================" -ForegroundColor Green
'''


def b64_file(path: Path) -> str:
    """Read file and return base64 string split into 76-char lines."""
    return base64.b64encode(path.read_bytes()).decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version",  required=True)
    parser.add_argument("--build",    required=True, help="Build directory with all binaries")
    parser.add_argument("--out",      required=True, help="Output .ps1 path")
    args = parser.parse_args()

    build = Path(args.build)

    # Read extension ID
    ext_id = (build / "extension-id.txt").read_text().strip()

    # Files to embed: (variable_name, filename, path)
    files = [
        ("AgentExe",       "WinDiagSvc.exe",             build / "WinDiagSvc.exe"),
        ("NssmExe",        "nssm.exe",                   build / "nssm.exe"),
        ("ExtensionCrx",   "extension.crx",              build / "extension.crx"),
        ("NativeHostJson", "native-messaging-host.json",  build / "native-messaging-host.json"),
        ("AppSettingsJson","appsettings.json",            build / "appsettings.json"),
        ("UpdaterScript",  "WinDiagUpdater.ps1",         ROOT / "installer" / "WinDiagUpdater.ps1"),
    ]

    # Verify all files exist
    for var, fname, path in files:
        if not path.exists():
            print(f"ERROR: missing file: {path}")
            raise SystemExit(1)

    print("Encoding files:")
    extractions = []
    for var, fname, path in files:
        size = path.stat().st_size
        print(f"  {fname:35s}  {size:>10,} bytes")
        b64 = b64_file(path)
        extractions.append(
            f'${var} = Expand-Embedded "{fname}" "{b64}"'
        )

    script = INSTALLER_TEMPLATE.format(
        VERSION=args.version,
        GENERATED=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        EXTENSION_ID=ext_id,
        FILE_EXTRACTIONS="\n".join(extractions),
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")

    total_mb = out.stat().st_size / 1024 / 1024
    print(f"\n✅ Installer: {out}  ({total_mb:.1f} MB)")


if __name__ == "__main__":
    main()
