# WinDiagUpdater.ps1
# Autonomous updater. Launched by HttpUpdateManager via Scheduled Task (SYSTEM).
# Runs as SYSTEM outside the agent process — safe to replace the exe.
# Agent itself runs as a per-user scheduled task (AtLogOn/Interactive), not a Windows Service.

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

# --- Stop the agent scheduled task (kills the running process) ---
Start-Sleep -Seconds 5
try {
    $task = Get-ScheduledTask -TaskName $ServiceName -ErrorAction Stop
    if ($task.State -eq "Running") {
        Write-Log "Stopping scheduled task $ServiceName..."
        Stop-ScheduledTask -TaskName $ServiceName
        Start-Sleep -Seconds 3
    }
} catch {
    Write-Log "Task not running or not found: $_"
}
# Also kill any lingering process by name
Get-Process -Name "WinDiagSvc" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

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
    Start-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
    exit 1
}

# --- Re-grant write access on appsettings.json after file replacement ---
try {
    icacls "$InstallDir\appsettings.json" /grant "BUILTIN\Users:(M)" | Out-Null
} catch { }

# --- Start the scheduled task (fires in active user sessions) ---
Write-Log "Starting scheduled task $ServiceName..."
try {
    Start-ScheduledTask -TaskName $ServiceName
    Write-Log "Task started successfully"
} catch {
    Write-Log "ERROR starting task: $_"
}

# --- Write ack to SMB ---
if ($AckDir -and (Test-Path $sharePath -ErrorAction SilentlyContinue)) {
    $ack = @{
        command_id   = "update"
        machine_id   = $machineId
        executed_at  = (Get-Date -Format "o")
        status       = "ok"
        message      = "Update completed and task restarted"
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
