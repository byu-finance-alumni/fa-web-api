<#
.SYNOPSIS
    Unattended weekly PROD backup: what the "FA Backup (prod, weekly)"
    Scheduled Task runs.

.DESCRIPTION
    Installed by scripts\backup-install-task.ps1 (api #535, plan Phases 4+5).
    It does four things and refuses to do a fifth.

      1. Resolves the repo by walking up from its own location, so it gives
         the same answer from wherever the task scheduler starts it.
      2. Loads the DPAPI-encrypted config from %LOCALAPPDATA%\fa-backups\
         config.xml and puts the values in BACKUP_* environment variables.
         Those variables exist in THIS process only: the task's PowerShell
         host exits when the script does, and nothing here writes them
         anywhere else. They are never printed.
      3. Runs  python scripts\backup_prod.py --incremental --keep <N>  and
         streams its output to %LOCALAPPDATA%\fa-backups\logs\<timestamp>.log
         (the newest 20 logs are kept).
      4. Exits with the Python script's exit code, so Task Scheduler's
         "Last Run Result" is the backup's verdict: 0 good, 1 a check failed,
         2 configuration or tooling, 3 unexpected.

    PROD ONLY, BY CONSTRUCTION. BACKUP_EXPECT_PROJECT_REF and
    BACKUP_SUPABASE_URL are literals in this file, not values read from the
    config. backup_prod.py refuses to run unless the database URL names that
    same project, so no edit to config.xml can turn this task into a backup
    of dev (or of anything else).

    What it never does: push, deploy, promote, write to the database or the
    bucket, or read anything outside BACKUP_DIR and the two prod endpoints.

    The failure marker lives next to the backups: BACKUP_DIR\LAST-RUN-FAILED.txt
    (written on failure, removed by the next success) and BACKUP_DIR\LAST-RUN.json
    (every run). If BACKUP_SLACK_WEBHOOK_URL was configured, a failure also
    posts one redacted line to Slack.

.PARAMETER Keep
    Passed through as --keep. Default 8.

.PARAMETER DryRun
    Pass --dry-run to the Python script: validates config and tooling, prints
    the plan, contacts nothing, writes nothing (except this script's log).

.PARAMETER ConfigPath
    Override the config file location (tests, a second account).

.EXAMPLE
    .\scripts\backup-scheduled.ps1 -DryRun
    .\scripts\backup-scheduled.ps1
#>

[CmdletBinding()]
param(
    [ValidateRange(1, 104)]
    [int] $Keep = 8,
    [switch] $DryRun,
    [string] $ConfigPath
)

$ErrorActionPreference = 'Stop'

# --- 1. where are we -------------------------------------------------------
# scripts\backup-scheduled.ps1 -> scripts -> the repo root.
$repoRoot = Split-Path -Parent $PSScriptRoot
$backupScript = Join-Path $repoRoot 'scripts\backup_prod.py'

$stateDir = Join-Path $env:LOCALAPPDATA 'fa-backups'
if (-not $ConfigPath) { $ConfigPath = Join-Path $stateDir 'config.xml' }
$logDir = Join-Path $stateDir 'logs'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyy-MM-dd-HHmmss'
$logPath = Join-Path $logDir "backup-$stamp.log"

function Write-Log {
    # Everything goes to the log AND to the console. Nothing that reaches
    # this function may carry a secret: the Python script redacts its own
    # output, and this script only ever logs paths, names and exit codes.
    param([string[]] $Lines)
    foreach ($line in $Lines) {
        Write-Output $line
        $line | Out-File -FilePath $script:logPath -Encoding utf8 -Append
    }
}

function Remove-OldLogs {
    # Keep the newest 20 logs. Names sort by time because of the stamp.
    param([int] $KeepLogs = 20)
    $logs = @(Get-ChildItem -Path $script:logDir -Filter 'backup-*.log' -File -ErrorAction SilentlyContinue |
        Sort-Object -Property Name -Descending)
    if ($logs.Count -gt $KeepLogs) {
        $logs | Select-Object -Skip $KeepLogs | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function ConvertFrom-Secure {
    param([securestring] $Secure)
    if ($null -eq $Secure) { return '' }
    return [System.Net.NetworkCredential]::new('', $Secure).Password
}

Write-Log @(
    "=== prod backup scheduled run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
    "repo:    $repoRoot"
    "config:  $ConfigPath"
    "keep:    $Keep"
    "mode:    $(if ($DryRun) { 'dry-run' } else { 'backup' })"
)

# --- 2. the config, decrypted for this process only ------------------------
if (-not (Test-Path $backupScript)) {
    Write-Log "ERROR: $backupScript not found. The task points at a checkout that no longer exists; re-run backup-install-task.ps1 from the main checkout."
    Remove-OldLogs
    exit 2
}
if (-not (Test-Path $ConfigPath)) {
    Write-Log "ERROR: no config at $ConfigPath. Run scripts\backup-install-task.ps1 once, as this Windows user."
    Remove-OldLogs
    exit 2
}

try {
    # Import-Clixml decrypts the SecureStrings with DPAPI. It only works in
    # the account that ran the installer, which is the point.
    $config = Import-Clixml -Path $ConfigPath
}
catch {
    Write-Log "ERROR: could not read $ConfigPath ($($_.Exception.GetType().Name)). Is the task running as the same Windows user that ran the installer?"
    Remove-OldLogs
    exit 2
}

$dbUrl = ConvertFrom-Secure $config.DatabaseUrl
$serviceKey = ConvertFrom-Secure $config.ServiceRoleKey
$webhook = ConvertFrom-Secure $config.SlackWebhookUrl
$backupDir = [string] $config.BackupDir

if (-not $dbUrl -or -not $serviceKey -or -not $backupDir) {
    Write-Log 'ERROR: the config is missing the database URL, the service key or BACKUP_DIR. Re-run backup-install-task.ps1.'
    Remove-OldLogs
    exit 2
}

# PROD ONLY. These two are literals on purpose; see the header. Do not move
# them into the config file.
$env:BACKUP_EXPECT_PROJECT_REF = 'njobhhdopwdodvzosrns'
$env:BACKUP_SUPABASE_URL = 'https://njobhhdopwdodvzosrns.supabase.co'

$env:BACKUP_DATABASE_URL = $dbUrl
$env:BACKUP_SUPABASE_SERVICE_ROLE_KEY = $serviceKey
$env:BACKUP_DIR = $backupDir
if ($webhook) { $env:BACKUP_SLACK_WEBHOOK_URL = $webhook } else { Remove-Item Env:BACKUP_SLACK_WEBHOOK_URL -ErrorAction SilentlyContinue }
$dbUrl = $null
$serviceKey = $null
$webhook = $null

Write-Log @(
    "backups: $backupDir"
    "secrets: loaded from config (not shown)"
    "slack:   $(if ($env:BACKUP_SLACK_WEBHOOK_URL) { 'failure notices on' } else { 'not configured' })"
)

# --- 3. find Python and run --------------------------------------------------
# backup_prod.py is standard-library only, so any Python 3.12+ will do and no
# virtualenv is needed. The py launcher is the reliable way to get "a Python
# 3" on Windows; plain `python` is the fallback (and may be the Store alias).
$python = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = 'py'
    $pythonArgs = @('-3')
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = 'python'
}
if (-not $python) {
    Write-Log 'ERROR: neither the py launcher nor python is on PATH for this account.'
    Remove-OldLogs
    exit 2
}

$scriptArgs = @($backupScript, '--incremental', '--keep', "$Keep")
if ($DryRun) { $scriptArgs += '--dry-run' }

# UTF-8 both ways so nothing the script prints is mangled in the log.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

Write-Log @("python:  $python $($pythonArgs -join ' ')", "command: backup_prod.py $($scriptArgs[1..($scriptArgs.Count - 1)] -join ' ')", '')

$exitCode = 1
Push-Location $repoRoot
try {
    # Stream, do not buffer: a 3-minute run should show progress in the log
    # as it happens, and a run killed by the 2h limit should leave a partial
    # log rather than none.
    & $python @pythonArgs @scriptArgs 2>&1 | ForEach-Object { Write-Log "$_" }
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 1 }
}
catch {
    Write-Log "ERROR: could not start Python ($($_.Exception.Message))"
    $exitCode = 2
}
finally {
    Pop-Location
    # Do not leave the secrets in this process's environment for longer than
    # the child needed them.
    foreach ($name in 'BACKUP_DATABASE_URL', 'BACKUP_SUPABASE_SERVICE_ROLE_KEY', 'BACKUP_SLACK_WEBHOOK_URL') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}

$verdict = if ($exitCode -eq 0) { 'OK' }
elseif ($DryRun) { 'FAILED (dry run: no marker written)' }
else { 'FAILED - see ' + (Join-Path $backupDir 'LAST-RUN-FAILED.txt') }
Write-Log @('', "exit:    $exitCode  ($verdict)")
Remove-OldLogs

exit $exitCode
