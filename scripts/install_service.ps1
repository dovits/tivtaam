# Register the Telegram bot as a *logon* scheduled task.
#
# Why a scheduled task and not a Windows service:
#   A service runs in session 0, which has no access to your desktop. This bot
#   needs a visible desktop twice over — `browser/auth.py` requires a headed
#   browser to clear the first captcha/OTP, and `runner.approve_job` parks the
#   browser at /cart so you can pay by hand. Neither is reachable from session
#   0, so an NSSM service can queue a cart you can never see or pay for.
#   This task runs as you, in your interactive session, at logon.
#
# Usage (no Administrator required — it registers under your own account):
#   .\scripts\install_service.ps1
#   .\scripts\install_service.ps1 -TaskName "TivtaamBot"
#   .\scripts\install_service.ps1 -Uninstall
#
# If you previously installed the NSSM service, remove it first:
#   nssm stop TivtaamBot confirm; nssm remove TivtaamBot confirm
#
# Idempotent: an existing task of the same name is replaced.

[CmdletBinding()]
param(
    [string]$TaskName = "TivtaamBot",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

if ($Uninstall) {
    Write-Host "Task '$TaskName' uninstalled."
    return
}

if (-not (Test-Path $VenvPython)) {
    throw "venv python not found at $VenvPython. Run 'python -m venv .venv' and 'pip install -e .' first."
}

# Fail early rather than registering a task that cannot start.
& $VenvPython -c "import tivtaam" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The venv at $VenvPython cannot import 'tivtaam'. Run 'pip install -e .' first."
}

$action = New-ScheduledTaskAction `
    -Execute $VenvPython `
    -Argument "-m tivtaam.bot.main" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"

# Interactive so the headed browser lands on your desktop. RunLevel Limited
# keeps it unelevated — the bot needs no privileges.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# ExecutionTimeLimit 0 = run indefinitely; a long-polling bot must not be
# killed after the default 3 days.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Tivtaam shopping bot (aiogram long-polling), interactive session." | Out-Null

Write-Host "Starting task..."
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Task '$TaskName' registered and started." -ForegroundColor Green
Write-Host "Logs:      $ProjectRoot\logs\runner-<date>.log"
Write-Host "Status:    Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Host "Stop:      Stop-ScheduledTask $TaskName"
Write-Host "Uninstall: .\scripts\install_service.ps1 -Uninstall"
Write-Host ""
Write-Host "Run '.venv\Scripts\python.exe -m tivtaam doctor' to confirm you can fill a cart."
