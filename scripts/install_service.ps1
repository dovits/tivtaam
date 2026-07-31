# Install the Telegram bot as a Windows service via NSSM.
#
# Prerequisite: NSSM (https://nssm.cc/download). Either:
#   - Put nssm.exe on PATH, or
#   - Pass -NssmPath "C:\path\to\nssm.exe"
#
# Usage (Run as Administrator):
#   .\scripts\install_service.ps1
#   .\scripts\install_service.ps1 -ServiceName "TivtaamBot"
#   .\scripts\install_service.ps1 -RunAsUser "$env:USERDOMAIN\$env:USERNAME"
#   .\scripts\install_service.ps1 -SkipPowerConfig   # leave sleep settings alone
#   .\scripts\install_service.ps1 -Uninstall
#
# This script is idempotent: it removes the existing service before installing.
#
# Hardening applied here (this box is the always-on host — see README
# "Running always-on"):
#   - restart on crash, with a throttle so a hard-failing bot can't spin
#   - Windows-level recovery actions as a second net under NSSM
#   - stdout/stderr log rotation, so logs\ can't fill the disk over months
#   - start only once the network stack is up (long-polling needs it)
#   - sleep/hibernate disabled, so the PC is actually reachable at 2am

[CmdletBinding()]
param(
    [string]$ServiceName = "TivtaamBot",
    [string]$NssmPath = "nssm",
    [string]$RunAsUser,
    [switch]$SkipPowerConfig,
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
    Write-Host "Note: power settings were not reverted. To allow sleep again:" -ForegroundColor Yellow
    Write-Host "  powercfg /change standby-timeout-ac 30"
    return
}

Write-Host "Installing service '$ServiceName'..."
& $NssmPath install $ServiceName $VenvPython "-m" "tivtaam.bot.main"
& $NssmPath set     $ServiceName AppDirectory $ProjectRoot
& $NssmPath set     $ServiceName Start        SERVICE_AUTO_START
& $NssmPath set     $ServiceName Description  "Tivtaam shopping bot (aiogram long-polling)."

# --- Startup ordering -------------------------------------------------------
# Long-polling fails immediately if DNS/TCP isn't up yet at boot. Depend on the
# network services, and use delayed auto-start so we're not racing the stack.
& $NssmPath set $ServiceName DependOnService Tcpip Dnscache
& $NssmPath set $ServiceName DelayedAutoStart 1

# --- Crash recovery ---------------------------------------------------------
# Restart on any exit, after 5s. AppThrottle marks a start as "failed" if the
# process dies within 10s — NSSM then backs off instead of hammering restarts,
# which is what turns a bad .env into a quiet outage rather than a log flood.
& $NssmPath set $ServiceName AppExit         Default Restart
& $NssmPath set $ServiceName AppRestartDelay 5000
& $NssmPath set $ServiceName AppThrottle     10000

# Second net: if NSSM itself dies, let the SCM restart the service. Reset the
# failure counter daily so transient blips don't exhaust the retry budget.
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null

# --- Logging ----------------------------------------------------------------
# Rotate at 10 MB, online (NSSM rotates without stopping the service) and keep
# appending across restarts so a crash loop doesn't erase the evidence.
& $NssmPath set $ServiceName AppStdout $(Join-Path $LogDir "bot.stdout.log")
& $NssmPath set $ServiceName AppStderr $(Join-Path $LogDir "bot.stderr.log")
& $NssmPath set $ServiceName AppStdoutCreationDisposition 4
& $NssmPath set $ServiceName AppStderrCreationDisposition 4
& $NssmPath set $ServiceName AppRotateFiles  1
& $NssmPath set $ServiceName AppRotateOnline 1
& $NssmPath set $ServiceName AppRotateBytes  10485760

# --- Environment ------------------------------------------------------------
# Unbuffered so a crash can't swallow the last log lines; UTF-8 so Hebrew
# product names survive the redirected (cp1252) stdout handle.
& $NssmPath set $ServiceName AppEnvironmentExtra PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# --- Shutdown ---------------------------------------------------------------
# Skip the window-message stages (WM_CLOSE=2, WM_QUIT=4) — a console Python app
# has no window to receive them, so they'd just burn 30s of timeout on every
# restart. Leaves Ctrl-C first, then terminate. Kill the tree so a runner
# subprocess mid-Playwright doesn't outlive the service.
& $NssmPath set $ServiceName AppStopMethodSkip     6
& $NssmPath set $ServiceName AppStopMethodConsole  5000
& $NssmPath set $ServiceName AppKillProcessTree    1

# --- Service account --------------------------------------------------------
# Default (LocalSystem) runs in session 0, which has no visible desktop. The
# runner launches Chrome *headful* by default (RUN_HEADLESS=false), and headful
# Chrome in session 0 is unreliable. Two ways out:
#   - run the service as your own account (-RunAsUser), or
#   - set RUN_HEADLESS=true in .env and accept the weaker fingerprint.
if ($RunAsUser) {
    Write-Host "Setting service account to $RunAsUser..."
    $cred = Get-Credential -UserName $RunAsUser -Message "Password for $RunAsUser (used only to set the service account)"
    $plain = $cred.GetNetworkCredential().Password
    & $NssmPath set $ServiceName ObjectName $RunAsUser $plain
    Write-Host "  Service will run as $RunAsUser."
} else {
    Write-Host "Service runs as LocalSystem (session 0)." -ForegroundColor Yellow
    Write-Host "  Headful Chrome is unreliable there. Either re-run with" -ForegroundColor Yellow
    Write-Host "  -RunAsUser '$env:USERDOMAIN\$env:USERNAME', or set" -ForegroundColor Yellow
    Write-Host "  RUN_HEADLESS=true in .env." -ForegroundColor Yellow
}

# --- Power ------------------------------------------------------------------
# A sleeping PC is an offline bot. Keep the machine awake on AC; the display may
# still sleep. Battery behaviour is left untouched for laptops.
if (-not $SkipPowerConfig) {
    Write-Host "Configuring power settings (no sleep on AC)..."
    & powercfg /change standby-timeout-ac 0
    & powercfg /change hibernate-timeout-ac 0
    Write-Host "  Sleep and hibernate timeouts disabled while on AC power."
    Write-Host "  Also enable 'Restore on AC Power Loss' in BIOS so the PC" -ForegroundColor Yellow
    Write-Host "  comes back by itself after an outage." -ForegroundColor Yellow
} else {
    Write-Host "Skipping power configuration (-SkipPowerConfig)." -ForegroundColor Yellow
}

Write-Host "Starting service..."
& $NssmPath start $ServiceName

Write-Host ""
Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
Write-Host "Logs: $LogDir\bot.stdout.log, bot.stderr.log (rotated at 10 MB)"
Write-Host "Check from your phone: send /health to the bot."
Write-Host "Manage with: Get-Service $ServiceName | Start-Service / Stop-Service / Restart-Service"
Write-Host "Uninstall:   .\scripts\install_service.ps1 -Uninstall"
