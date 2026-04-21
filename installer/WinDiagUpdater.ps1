# WinDiagUpdater.ps1
# Autonomous updater. Launched by UpdateManager via Scheduled Task.
# Runs as LocalSystem outside the service process — safe to replace the exe.

param()

$ServiceName  = "WinDiagSvc"
$InstallDir   = "C:\Program Files\Windows Diagnostics"
$StagingDir   = Join-Path $InstallDir "staging"
$AckDir       = $null   # resolved below from appsettings.json

function Write-Log { param($Msg) Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Msg" }

Write-Log "WinDiagUpdater started"

# --- Read share path and machine_id from appsettings.json ---
$settingsPath = Join-Path $InstallDir "appsettings.json"
if (Test-Path $settingsPath) {
    $cfg       = Get-Content $settingsPath -Raw | ConvertFrom-Json
    $sharePath = $cfg.AgentSettings.SharePath
    $machineId = $cfg.AgentSettings.MachineId
    if ($sharePath -and $machineId) {
        $AckDir = Join-Path $sharePath "$machineId\cmd"
    }
}

# --- Wait for service to fully stop ---
Start-Sleep -Seconds 5
try {
    $svc = Get-Service -Name $ServiceName -ErrorAction Stop
    if ($svc.Status -ne "Stopped") {
        Write-Log "Stopping $ServiceName..."
        Stop-Service -Name $ServiceName -Force
        $svc.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
    }
} catch {
    Write-Log "Service already stopped or not found: $_"
}

# --- Replace files ---
if (-not (Test-Path $StagingDir)) {
    Write-Log "ERROR: staging dir not found — aborting"
    exit 1
}

Write-Log "Copying staged files to $InstallDir"
try {
    Get-ChildItem -Path $StagingDir | ForEach-Object {
        $dest = Join-Path $InstallDir $_.Name
        Copy-Item -Path $_.FullName -Destination $dest -Force
    }
} catch {
    Write-Log "ERROR copying files: $_"
    # Try to restart old version anyway
    Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
    exit 1
}

# --- Start service ---
Write-Log "Starting $ServiceName..."
try {
    Start-Service -Name $ServiceName
    Write-Log "Service started successfully"
} catch {
    Write-Log "ERROR starting service: $_"
}

# --- Write ack to SMB ---
if ($AckDir -and (Test-Path $sharePath -ErrorAction SilentlyContinue)) {
    $ack = @{
        command_id   = "update"
        machine_id   = $machineId
        executed_at  = (Get-Date -Format "o")
        status       = "ok"
        message      = "Update completed and service restarted"
    } | ConvertTo-Json
    try {
        if (-not (Test-Path $AckDir)) { New-Item -ItemType Directory -Path $AckDir -Force | Out-Null }
        $ack | Set-Content -Path (Join-Path $AckDir "ack.json") -Encoding UTF8
        Write-Log "Ack written to SMB"
    } catch {
        Write-Log "Could not write ack: $_"
    }
}

# --- Cleanup ---
Remove-Item -Path $StagingDir -Recurse -Force -ErrorAction SilentlyContinue

# --- Remove this scheduled task ---
schtasks /Delete /TN "WinDiagUpdate" /F 2>$null

Write-Log "WinDiagUpdater completed"
