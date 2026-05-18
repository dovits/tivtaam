# Install the Telegram bot as a Windows service via NSSM.
#
# Prerequisite: NSSM (https://nssm.cc/download). Either:
#   - Put nssm.exe on PATH, or
#   - Pass -NssmPath "C:\path\to\nssm.exe"
#
# Usage (Run as Administrator):
#   .\scripts\install_service.ps1
#   .\scripts\install_service.ps1 -ServiceName "TivtaamBot"
#   .\scripts\install_service.ps1 -Uninstall
#
# This script is idempotent: it removes the existing service before installing.

[CmdletBinding()]
param(
    [string]$ServiceName = "TivtaamBot",
    [string]$NssmPath = "nssm",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir      = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $VenvPython)) {
    throw "venv python not found at $VenvPython. Run 'python -m venv .venv' and 'pip install -e .' first."
}
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

function Test-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    throw "This script must be run as Administrator (NSSM service operations require it)."
}

# Verify nssm.exe is reachable
try {
    & $NssmPath version | Out-Null
} catch {
    throw "nssm not found. Download from https://nssm.cc and either add to PATH or pass -NssmPath."
}

# Remove any prior installation
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing service '$ServiceName'..."
    & $NssmPath stop    $ServiceName confirm | Out-Null
    & $NssmPath remove  $ServiceName confirm | Out-Null
}

if ($Uninstall) {
    Write-Host "Service '$ServiceName' uninstalled."
    return
}

Write-Host "Installing service '$ServiceName'..."
& $NssmPath install $ServiceName $VenvPython "-m" "tivtaam.bot.main"
& $NssmPath set     $ServiceName AppDirectory $ProjectRoot
& $NssmPath set     $ServiceName AppStdout    (Join-Path $LogDir "bot.stdout.log")
& $NssmPath set     $ServiceName AppStderr    (Join-Path $LogDir "bot.stderr.log")
& $NssmPath set     $ServiceName Start        SERVICE_AUTO_START
& $NssmPath set     $ServiceName AppExit      Default Restart
& $NssmPath set     $ServiceName AppRestartDelay 5000
& $NssmPath set     $ServiceName Description  "Tivtaam shopping bot (aiogram long-polling)."

Write-Host "Starting service..."
& $NssmPath start $ServiceName

Write-Host ""
Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
Write-Host "Logs: $LogDir\bot.stdout.log, bot.stderr.log"
Write-Host "Manage with: Get-Service $ServiceName | Start-Service / Stop-Service / Restart-Service"
Write-Host "Uninstall:   .\scripts\install_service.ps1 -Uninstall"
