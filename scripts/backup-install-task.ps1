<#
.SYNOPSIS
    One-time setup for the weekly PROD backup: store the secrets (DPAPI) and
    register the Scheduled Task that runs scripts\backup-scheduled.ps1.

.DESCRIPTION
    Run this once, by hand, in a PowerShell window (api #535, plan Phase 4).
    It asks for four things and saves them ENCRYPTED TO YOUR WINDOWS ACCOUNT
    (DPAPI, via Export-Clixml of SecureStrings):

        1. the prod SESSION-pooler database URL (port 5432, password included)
        2. the prod service_role key
        3. BACKUP_DIR - a local, unsynced folder OUTSIDE the repo
        4. an optional Slack incoming-webhook URL (failure notices only)

    to  %LOCALAPPDATA%\fa-backups\config.xml

    and then registers the task "FA Backup (prod, weekly)": every Sunday at
    03:00 local time, wake the machine if asleep, run as soon as possible if
    the time was missed, only when the network is up, give up after 2 hours.

    THIS TASK BACKS UP PROD ONLY. The project ref is not something you enter:
    backup-scheduled.ps1 carries the prod ref as a literal, and this script
    refuses a database URL that names the dev project or fails to name prod.
    The dev sandbox has nothing worth a weekly copy and a task that could be
    pointed at it by editing a config file is a task that will be, one day,
    by accident.

    "Run whether user is logged on or not": the task has to run at 03:00 on a
    Sunday with nobody signed in, so it is registered with your Windows
    password (Windows stores it, this script does not). DPAPI secrets only
    decrypt inside the SAME Windows account that saved them, which is also
    why the task must run as you and not as SYSTEM or another user. If you
    change your Windows password, re-run this script.

    Re-running is safe: the config is overwritten and the task is replaced.

    What this task never does: push, deploy, promote, or touch anything but
    the prod database (read-only via pg_dump/psql), the headshots bucket
    (read-only), and BACKUP_DIR.

.PARAMETER TaskName
    Scheduled Task name. Default 'FA Backup (prod, weekly)'.

.PARAMETER At
    Time of day, LOCAL time. Default 03:00.

.PARAMETER DayOfWeek
    Day of the week. Default Sunday.

.PARAMETER Keep
    How many OK runs to keep in BACKUP_DIR; older ones are deleted after each
    successful run (backup_prod.py --keep). Default 8 (two months of weeklies).
    To change it later, re-run this script with a different -Keep.

.PARAMETER Uninstall
    Remove the task AND the stored secrets. Backups already taken are left
    where they are; the logs folder is left too.

.EXAMPLE
    .\scripts\backup-install-task.ps1
    .\scripts\backup-install-task.ps1 -Keep 12
    .\scripts\backup-install-task.ps1 -WhatIf
    .\scripts\backup-install-task.ps1 -Uninstall
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string] $TaskName = 'FA Backup (prod, weekly)',
    [string] $At = '03:00',
    [ValidateSet('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')]
    [string] $DayOfWeek = 'Sunday',
    [ValidateRange(1, 104)]
    [int] $Keep = 8,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

# The two Supabase project refs. PROD is what this task backs up; DEV is named
# here only so that it can be REFUSED by name with a plain message.
$prodRef = 'njobhhdopwdodvzosrns'
$devRef = 'tnnhhnzglyfqolxdojyb'

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'The ScheduledTasks module is not available. This script is Windows-only.'
}

$configDir = Join-Path $env:LOCALAPPDATA 'fa-backups'
$configPath = Join-Path $configDir 'config.xml'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# --- uninstall ---------------------------------------------------------------
if ($Uninstall) {
    if ($existing) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "Removed scheduled task '$TaskName'."
        }
    }
    else {
        Write-Output "No scheduled task named '$TaskName' -- nothing to remove."
    }
    if (Test-Path $configPath) {
        if ($PSCmdlet.ShouldProcess($configPath, 'Delete stored backup secrets')) {
            Remove-Item -Path $configPath -Force
            Write-Output "Deleted $configPath (the stored secrets)."
        }
    }
    else {
        Write-Output "No stored config at $configPath -- nothing to delete."
    }
    Write-Output 'Backups already taken and the logs folder were left in place.'
    return
}

# --- where the runner is -----------------------------------------------------
$runner = Join-Path $PSScriptRoot 'backup-scheduled.ps1'
$backupScript = Join-Path $PSScriptRoot 'backup_prod.py'
if (-not (Test-Path $runner) -or -not (Test-Path $backupScript)) {
    throw "Cannot find backup-scheduled.ps1 / backup_prod.py next to this script -- run it from the repo it was committed to."
}
$repoRoot = Split-Path -Parent $PSScriptRoot
if ($repoRoot -match 'worktrees') {
    Write-Warning "This checkout looks like a git worktree ($repoRoot). The task will run the script from THIS path; worktrees get deleted. Run the installer from the main checkout."
}

# The pg client tools are a hard requirement of the Python script; say so now
# rather than at 03:00 on Sunday.
foreach ($tool in 'pg_dump', 'pg_restore', 'psql') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Warning "$tool is not on PATH for this session. The task inherits the user PATH; make sure C:\Program Files\PostgreSQL\17\bin is in it or every run will fail."
    }
}

# --- helpers -----------------------------------------------------------------
function Read-Secret {
    # Read-Host -AsSecureString never shows the characters and never puts them
    # in the console history. The plain text is only ever materialised for
    # validation, in a local that is cleared straight after.
    param([string] $Prompt)
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    return $secure
}

function ConvertFrom-Secure {
    param([securestring] $Secure)
    if ($null -eq $Secure) { return '' }
    return [System.Net.NetworkCredential]::new('', $Secure).Password
}

# --- 1. the database URL -----------------------------------------------------
Write-Output ''
Write-Output 'Prod SESSION-pooler database URL.'
Write-Output '  Supabase dashboard > the PROD project > Connect > Method "Session pooler".'
Write-Output '  Copy the URI, replace [YOUR-PASSWORD], percent-encode @ / : # in the password.'
Write-Output '  It must end in :5432/postgres. Nothing you type is shown.'
$dbUrlSecure = Read-Secret -Prompt 'BACKUP_DATABASE_URL'
$plain = ConvertFrom-Secure $dbUrlSecure
try {
    if (-not $plain) { throw 'No database URL entered.' }
    if ($plain -notmatch '^postgres(ql)?://') { throw 'The database URL must start with postgresql://.' }
    if ($plain -match $devRef) {
        throw "That URL names the DEV project ($devRef). This task backs up prod only; nothing was saved."
    }
    if ($plain -notmatch $prodRef) {
        throw "That URL does not name the prod project ($prodRef). This task backs up prod only; nothing was saved."
    }
    if ($plain -notmatch ':5432(/|$)') {
        throw 'That URL is not on port 5432 (the SESSION pooler). pg_dump cannot use the transaction pooler (:6543). Nothing was saved.'
    }
}
finally {
    $plain = $null
}

# --- 2. the service role key -------------------------------------------------
Write-Output ''
Write-Output 'Prod service_role key (Project Settings > API keys). Nothing you type is shown.'
$keySecure = Read-Secret -Prompt 'BACKUP_SUPABASE_SERVICE_ROLE_KEY'
if ((ConvertFrom-Secure $keySecure).Length -lt 20) {
    throw 'That does not look like a service_role key (too short). Nothing was saved.'
}

# --- 3. the destination ------------------------------------------------------
Write-Output ''
Write-Output 'BACKUP_DIR: a LOCAL, UNSYNCED folder outside the repo, e.g. D:\fa-backups.'
Write-Output '  Not OneDrive, not Documents/Desktop if those redirect into OneDrive. BitLocker on.'
$backupDir = (Read-Host -Prompt 'BACKUP_DIR').Trim().Trim('"')
if (-not $backupDir) { throw 'No BACKUP_DIR entered.' }
if (-not [System.IO.Path]::IsPathRooted($backupDir)) { throw 'BACKUP_DIR must be an absolute path.' }
$backupDir = [System.IO.Path]::GetFullPath($backupDir)
$root = [System.IO.Path]::GetPathRoot($backupDir)
if ($backupDir.TrimEnd('\') -eq $root.TrimEnd('\')) {
    throw 'BACKUP_DIR must not be a drive root: pruning refuses to run there.'
}
if ($backupDir.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'BACKUP_DIR must be OUTSIDE the repository.'
}
if ($backupDir -match '(?i)onedrive|dropbox|icloud|google drive|\\box\\') {
    throw 'BACKUP_DIR looks cloud-synced. A dump of every alumnus plus the staff auth schema must not replicate to a sync service.'
}
if (-not (Test-Path $backupDir)) {
    if ($PSCmdlet.ShouldProcess($backupDir, 'Create backup folder')) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    }
}

# --- 4. optional Slack webhook -----------------------------------------------
Write-Output ''
Write-Output 'Optional: a Slack incoming-webhook URL for FAILURE notices (press Enter to skip).'
Write-Output '  Nothing is posted on success. The URL is a credential and is stored encrypted too.'
$hookSecure = Read-Secret -Prompt 'BACKUP_SLACK_WEBHOOK_URL'
$hookPlain = ConvertFrom-Secure $hookSecure
$hasHook = [bool] $hookPlain
if ($hasHook -and $hookPlain -notmatch '^https://') {
    $hookPlain = $null
    throw 'The webhook URL must start with https://. Nothing was saved.'
}
$hookPlain = $null

# --- 5. save the config, encrypted to this Windows account --------------------
# Export-Clixml serialises a SecureString with DPAPI (CurrentUser scope): the
# file is unreadable from any other account and from any other machine. The
# plain values (BackupDir, timestamps) are not secrets.
$config = @{
    DatabaseUrl     = $dbUrlSecure
    ServiceRoleKey  = $keySecure
    SlackWebhookUrl = if ($hasHook) { $hookSecure } else { $null }
    BackupDir       = $backupDir
    SavedAt         = (Get-Date).ToString('o')
    SavedBy         = [Security.Principal.WindowsIdentity]::GetCurrent().Name
}
if ($PSCmdlet.ShouldProcess($configPath, 'Save encrypted backup config')) {
    if (-not (Test-Path $configDir)) {
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    }
    $config | Export-Clixml -Path $configPath -Force
    Write-Output ''
    Write-Output "Saved encrypted config to $configPath"
}

# --- 6. the scheduled task ---------------------------------------------------
# Prefer PowerShell 7 when it is installed; fall back to Windows PowerShell.
$pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
$shell = if ($pwshCommand) { $pwshCommand.Source } else { 'powershell.exe' }

$runnerArgs = @(
    '-NoProfile'
    '-NonInteractive'
    '-ExecutionPolicy', 'Bypass'
    '-File', "`"$runner`""
    '-Keep', "$Keep"
)
$action = New-ScheduledTaskAction -Execute $shell -Argument ($runnerArgs -join ' ') `
    -WorkingDirectory $repoRoot

# Local time; Windows handles daylight saving on its own.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$description = @(
    "Weekly PROD backup of the Finance Alumni Database (Supabase project $prodRef): database, auth schema, headshots bucket."
    "Runs scripts\backup-scheduled.ps1 -> backup_prod.py --incremental --keep $Keep. Secrets: DPAPI config in %LOCALAPPDATA%\fa-backups."
    'Read-only against prod. Never pushes, deploys, or touches dev.'
) -join ' '

$target = "$TaskName ($DayOfWeek $At local, as $user) -> $shell $runner -Keep $Keep"
if ($PSCmdlet.ShouldProcess($target, 'Register scheduled task')) {
    Write-Output ''
    Write-Output 'The task must run while you are signed out, so Windows needs your account password'
    Write-Output '(Windows stores it in the task, this script does not). Nothing you type is shown.'
    $winPwSecure = Read-Host -Prompt "Windows password for $user" -AsSecureString
    $winPw = ConvertFrom-Secure $winPwSecure
    try {
        if (-not $winPw) { throw 'No password entered; the task was not registered. The config was saved.' }
        if ($existing) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        # -User/-Password = logon type Password = "run whether user is logged on
        # or not". S4U ("do not store password") is not used on purpose: it
        # runs without the user profile, and DPAPI needs the profile to find
        # the master key that decrypts config.xml.
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description $description `
            -User $user -Password $winPw -RunLevel Limited | Out-Null
    }
    finally {
        $winPw = $null
    }

    Write-Output ''
    Write-Output "Registered '$TaskName': every $DayOfWeek at $At LOCAL time, as $user, keep $Keep OK runs."
    Write-Output "Backups land in:        $backupDir"
    Write-Output "Logs:                   $(Join-Path $configDir 'logs')"
    Write-Output "Failure marker:         $(Join-Path $backupDir 'LAST-RUN-FAILED.txt')"
    Write-Output "Run it once now:        Start-ScheduledTask -TaskName '$TaskName'"
    Write-Output "Last result:            Get-ScheduledTaskInfo -TaskName '$TaskName'"
    Write-Output "Change how many to keep: .\scripts\backup-install-task.ps1 -Keep 12"
    Write-Output "Remove it:              .\scripts\backup-install-task.ps1 -Uninstall"
}
