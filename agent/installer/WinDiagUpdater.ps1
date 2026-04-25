# WinDiagUpdater.ps1
# Autonomous updater. Launched by HttpUpdateManager via Scheduled Task (SYSTEM).
# Runs as SYSTEM outside the agent process — safe to replace the exe.

param()

$ServiceName = "WinDiagSvc"
$InstallDir  = "C:\Program Files\Windows Diagnostics"
$StagingDir  = Join-Path $InstallDir "staging"

function Write-Log { param($Msg) Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Msg" }

Write-Log "WinDiagUpdater started"

Start-Sleep -Seconds 3

# Stop scheduled task and kill any lingering process
schtasks /End /TN $ServiceName 2>$null
Start-Sleep -Seconds 1
taskkill /F /IM "Seamlean.Agent.exe" 2>$null
Start-Sleep -Seconds 1

# Verify staged exe exists
$StagedExe = Join-Path $StagingDir "Seamlean.Agent.exe"
if (-not (Test-Path $StagedExe)) {
    Write-Log "ERROR: staged Seamlean.Agent.exe not found — aborting"
    schtasks /Run /TN $ServiceName 2>$null
    exit 1
}

# Replace exe
Write-Log "Copying Seamlean.Agent.exe to $InstallDir"
try {
    Copy-Item -Path $StagedExe -Destination (Join-Path $InstallDir "Seamlean.Agent.exe") -Force
} catch {
    Write-Log "ERROR copying file: $_"
    schtasks /Run /TN $ServiceName 2>$null
    exit 1
}

# Re-grant write access on appsettings.json for interactive users
try {
    icacls "$InstallDir\appsettings.json" /grant "BUILTIN\Users:(M)" | Out-Null
} catch { }

# Cleanup staging
Remove-Item -Path $StagingDir -Recurse -Force -ErrorAction SilentlyContinue

# Start agent
Write-Log "Starting scheduled task $ServiceName"
schtasks /Run /TN $ServiceName 2>$null

# Remove this update task
schtasks /Delete /TN "WinDiagUpdate" /F 2>$null

Write-Log "WinDiagUpdater completed"
