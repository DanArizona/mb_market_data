# Experimental, opt-in Windows Task Scheduler reminder installer.
# DEFAULT IS PLAN ONLY. No task is created unless -Install is explicitly used,
# production checkout is on main, and the user types the exact confirmation.
param(
    [switch]$Install,
    [string]$PythonwExe = "$env:USERPROFILE\anaconda3\envs\sea-green\pythonw.exe"
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$probe = Join-Path $root 'probes\closeout_readiness_reminder.py'
$taskName = 'mb_package_Closeout_Reminder'
$principalName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not (Test-Path -LiteralPath $PythonwExe -PathType Leaf)) {
    throw "pythonw.exe not found: $PythonwExe"
}
if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) {
    throw "Reminder probe not found: $probe"
}
$argsText = '"' + $probe + '" --notify --market-data-root "' + $root + '"'
Write-Host "CLOSEOUT REMINDER TASK PLAN"
Write-Host "Task name        : $taskName"
Write-Host "Pythonw          : $PythonwExe"
Write-Host "Script           : $probe"
Write-Host "Market-data root : $root"
Write-Host "Logon            : only when $principalName is logged on"
Write-Host "Schedule         : hourly; reminder probe checks America/New_York 16:10+"
Write-Host "Effect           : read-only readiness check + topmost reminder ONLY if DUE"
Write-Host "No unattended Schwab call or password handling."
if (-not $Install) {
    Write-Host "PLAN ONLY. Task Scheduler has not been changed."
    exit 0
}
$branch = (& git -C $root branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
    throw "INSTALL REFUSED: must be run from approved installed main checkout; found '$branch'"
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "INSTALL REFUSED: existing reminder task will not be overwritten"
}
Write-Host "You are about to create a persistent, interactive, hourly reminder task."
$answer = Read-Host "Type INSTALL CLOSEOUT REMINDER to proceed"
if ($answer -cne 'INSTALL CLOSEOUT REMINDER') {
    Write-Host "Cancelled. No task created."
    exit 2
}
$action = New-ScheduledTaskAction -Execute $PythonwExe -Argument $argsText -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $principalName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Attended-only mb_package post-close readiness reminder; no provider authentication.'
Write-Host "Scheduled reminder installed. Verify with Get-ScheduledTask -TaskName $taskName."
