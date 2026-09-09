<#
.SYNOPSIS
    Register (or remove) the twice-daily change-request Scheduled Task.

.DESCRIPTION
    Wraps change-requests-scheduled.ps1 in a Windows Scheduled Task. It
    registers nothing on its own beyond what you ask for, and it supports
    -WhatIf, so the honest first run is:

        .\scripts\change-requests-install-task.ps1 -WhatIf

    Defaults: twice a day at 13:00 and 20:00 LOCAL time, running as the current
    user, only while logged on, in DRY-RUN mode.

    The two times are Jake's, and they are parameters rather than literals so he
    can shift them without editing this file. 13:00 catches whatever he approved
    that morning, so it does not wait for tomorrow. 20:00 does its work in the
    evening, so the results are waiting for him when he starts the next day.
    There is deliberately no early-morning run.

    The evening run finishes with nobody watching, which is why the run digest
    has to stand on its own -- see the digest section of docs/CHANGE-REQUESTS.md.

    NOTE: a Windows Scheduled Task trigger is LOCAL time and follows daylight
    saving on its own. This repo's GitHub Actions crons are UTC and do not.

    Dry run is the default for the same reason it is the default in the
    scheduled script itself -- `request next --execute` starts a clock, and a
    clock that starts at 20:00 for work opened at 08:00 records twelve hours
    that never happened. Pass -Execute once a Claude Code session is genuinely
    wired to consume the plan.

    Run it by hand for a week before registering anything:

        .\scripts\change-requests-scheduled.ps1

    To remove it later:

        .\scripts\change-requests-install-task.ps1 -Unregister

    This task never pushes, never deploys, and never touches production data.
    It runs one offline CLI over a folder of Markdown files.

.PARAMETER TaskName
    Scheduled Task name. Default 'FinanceAlumniDB-ChangeRequests'.

.PARAMETER AfternoonTime
    First run of the day, local time. Default 13:00.

.PARAMETER EveningTime
    Second run of the day, local time. Default 20:00.

.PARAMETER Execute
    Register the task in execute mode rather than dry run.

.PARAMETER Limit
    Requests one run may pick up. Default 1.

.PARAMETER Repo
    Default repository for branch creation. Default fa-web-api.

.PARAMETER Unregister
    Remove the task instead of creating it.

.EXAMPLE
    .\scripts\change-requests-install-task.ps1 -WhatIf
    .\scripts\change-requests-install-task.ps1
    .\scripts\change-requests-install-task.ps1 -AfternoonTime '12:30' -EveningTime '21:00'
    .\scripts\change-requests-install-task.ps1 -Unregister
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string] $TaskName = 'FinanceAlumniDB-ChangeRequests',
    [string] $AfternoonTime = '13:00',
    [string] $EveningTime = '20:00',
    [switch] $Execute,
    [ValidateRange(1, 50)]
    [int] $Limit = 1,
    [ValidateSet('fa-web-api', 'fa-web-app')]
    [string] $Repo = 'fa-web-api',
    [switch] $Unregister
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'The ScheduledTasks module is not available. This script is Windows-only.'
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# --- remove ----------------------------------------------------------------
if ($Unregister) {
    if (-not $existing) {
        Write-Output "No scheduled task named '$TaskName' -- nothing to remove."
        return
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    }
    return
}

# --- register --------------------------------------------------------------
$runner = Join-Path $PSScriptRoot 'change-requests-scheduled.ps1'
if (-not (Test-Path $runner)) {
    throw "Cannot find $runner -- run this from the repo it was committed to."
}

# Prefer PowerShell 7 when it is installed; fall back to Windows PowerShell.
# Written the long way on purpose: `?.` is a PowerShell 7 operator, and this
# script has to PARSE under Windows PowerShell 5.1 to be able to say so.
$pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
$shell = if ($pwshCommand) { $pwshCommand.Source } else { 'powershell.exe' }

$runnerArgs = @(
    '-NoProfile'
    '-NonInteractive'
    '-ExecutionPolicy', 'Bypass'
    '-File', "`"$runner`""
    '-Limit', "$Limit"
    '-Repo', $Repo
)
if ($Execute) { $runnerArgs += '-Execute' }

$action = New-ScheduledTaskAction -Execute $shell -Argument ($runnerArgs -join ' ') `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)

# Local time, and daylight saving is handled by Windows. (The GitHub Actions
# crons in this repo are UTC and are NOT the same thing.)
$times = @($AfternoonTime, $EveningTime)
$triggers = foreach ($time in $times) { New-ScheduledTaskTrigger -Daily -At $time }

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

$description = @(
    "Change-request intake: import Outlook .msg files and report approved work."
    "Mode: $(if ($Execute) { 'execute' } else { 'dry-run' }). Limit: $Limit. Repo: $Repo."
    'Never pushes, deploys, or touches production data.'
) -join ' '

$target = "$TaskName ($($times -join ', ') local) -> $shell $runner"
if ($PSCmdlet.ShouldProcess($target, 'Register scheduled task')) {
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal -Description $description | Out-Null

    Write-Output "Registered '$TaskName' at $($times -join ' and ') LOCAL time, in $(if ($Execute) { 'execute' } else { 'dry-run' }) mode."
    Write-Output "Run it once by hand:  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Output "Check the last result: Get-ScheduledTaskInfo -TaskName '$TaskName'"
    Write-Output "Remove it:             .\scripts\change-requests-install-task.ps1 -Unregister"
}
