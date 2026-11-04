# Experimental Windows Task Scheduler reminder candidate.
# PLAN ONLY by default. Installation requires a separately approved production
# checkout, a qualified Python interpreter, and explicit operator confirmation.
param(
    [switch]$Install,
    [string]$ExpectedInstalledRoot = "",
    [string]$PythonwExe = "$env:USERPROFILE\anaconda3\envs\sea-green\pythonw.exe"
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$probe = Join-Path $root 'probes\closeout_readiness_reminder.py'
$taskName = 'mb_package_Closeout_Reminder'
$principalName = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$now = Get-Date
# Align checks to HH:10 each hour in the local Windows clock. ET and
# U.S. time zones have hour-based offsets, so the check is also at :10 ET.
# The Python exchange calendar is the source of truth for when due.
$firstTick = $now.Date.AddHours($now.Hour).AddMinutes(10)
if ($firstTick -le $now) {
    $firstTick = $firstTick.AddHours(1)
}
if (-not (Test-Path -LiteralPath $PythonwExe -PathType Leaf)) {
    throw "pythonw.exe not found: $PythonwExe"
}
$pythonExe = Join-Path (Split-Path -Parent $PythonwExe) 'python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Matching python.exe not found: $pythonExe"
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
Write-Host "First check      : $firstTick (Windows local time)"
Write-Host "Schedule         : every hour at HH:10 local; due gate uses XNYS close + 10min ET"
Write-Host "Recovery alert   : after 07:00 ET next trading day for missing prior closeout"
Write-Host "Effect           : desktop reminder ONLY if DUE or RECOVERY_REQUIRED"
Write-Host "No unattended Schwab call, password handling or Pushover transport."
if (-not $Install) {
    Write-Host "PLAN ONLY. Task Scheduler has not been changed."
    if ((& git -C $root branch --show-current).Trim() -ne 'main') {
        Write-Host "EXPERIMENTAL CHECKOUT: these paths are for review, NOT production installation."
    }
    exit 0
}
if ([string]::IsNullOrWhiteSpace($ExpectedInstalledRoot)) {
    throw "INSTALL REFUSED: explicit -ExpectedInstalledRoot is required after production approval"
}
$approved = (Resolve-Path -LiteralPath $ExpectedInstalledRoot).Path
if ($root -ine $approved) {
    throw "INSTALL REFUSED: script root '$root' is not approved target '$approved'"
}
$branch = (& git -C $root branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
    throw "INSTALL REFUSED: requires the approved installed main checkout; found '$branch'"
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "INSTALL REFUSED: existing task will not be overwritten"
}
# Pythonw may hide import errors. Preflight the corresponding python.exe
# before installing a silent scheduled task. Pin current qualified version.
$packageInfo = & $pythonExe -m pip show exchange-calendars 2>&1
if ($LASTEXITCODE -ne 0 -or -not ($packageInfo -match '^Version: 4\.13\.2$')) {
    throw "INSTALL REFUSED: exchange-calendars 4.13.2 is not installed in $pythonExe"
}
& $pythonExe -c 'import exchange_calendars'
if ($LASTEXITCODE -ne 0) {
    throw "INSTALL REFUSED: exchange-calendars import preflight failed"
}
Write-Host "You are about to create a persistent interactive hourly reminder task."
$answer = Read-Host "Type INSTALL CLOSEOUT REMINDER to proceed"
if ($answer -cne 'INSTALL CLOSEOUT REMINDER') {
    Write-Host "Cancelled. No task created."
    exit 2
}
$action = New-ScheduledTaskAction -Execute $PythonwExe -Argument $argsText -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At $firstTick -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $principalName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Desktop reminder for unattended closeout; no API or secret access.'
Write-Host "Scheduled reminder installed. Verify with Get-ScheduledTask -TaskName $taskName."
