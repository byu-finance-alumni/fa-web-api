<#
.SYNOPSIS
    Unattended change-request run: import, then `request next`.

.DESCRIPTION
    This is what the Windows Scheduled Task invokes. It does four things and
    refuses to do a fifth.

      1. Resolves the repo and the change-request data folder by walking up
         from its own location. No absolute path is hard-coded, because this
         script has to give the same answer from a worktree, from the main
         checkout, and from whatever directory the task scheduler happens to
         start it in.
      2. Exits IMMEDIATELY and QUIETLY when there is nothing to do. Most days
         approved/ is empty and inbox-msg/ is empty, and the common case has to
         cost nothing: no python start-up, no log file, no output.
      3. Runs `request import` and then `request next`, and tees everything to
         change-requests/runs/scheduled-<timestamp>.log.
      4. Returns the CLI's exit code.

    What it will never do: push, deploy, promote, touch production data, or
    work anything outside approved/. It only ever invokes the intake CLI, and
    that CLI has no code path to any of them.

    DEFAULT IS -WhatIf-SHAPED. Without -Execute this is a dry run: it reports
    what would be picked up and changes nothing. That is deliberate. `request
    next --execute` starts the clock (claude_started, a branch, an In Progress
    row), and a run that clocks in at 08:00 when nobody opens the plan until
    16:00 records eight hours of "Claude runtime" that never happened. Add
    -Execute only once a session is genuinely wired to consume the plan.

.PARAMETER Execute
    Clock the selected requests in and write a run digest. Off by default.

.PARAMETER Limit
    How many requests one run may pick up. Default 1.

.PARAMETER Repo
    Default repository for branch creation. A request may override it with a
    `Target Repo:` line.

.PARAMETER NoBranch
    Record branch names without creating them.

.PARAMETER CrHome
    Override the change-request data folder (same as the CR_HOME env var).
    Named CrHome, not Home: $HOME is a PowerShell automatic variable and a
    parameter of that name is a trap waiting for whoever edits this next.

.EXAMPLE
    .\scripts\change-requests-scheduled.ps1
    .\scripts\change-requests-scheduled.ps1 -Execute -Limit 2
#>

[CmdletBinding()]
param(
    [switch] $Execute,
    [ValidateRange(1, 50)]
    [int] $Limit = 1,
    [ValidateSet('fa-web-api', 'fa-web-app')]
    [string] $Repo = 'fa-web-api',
    [switch] $NoBranch,
    [string] $CrHome
)

$ErrorActionPreference = 'Stop'

# --- 1. where are we -------------------------------------------------------
# scripts/change-requests-scheduled.ps1 -> scripts -> the repo root.
$repoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ChangeRequestHome {
    <#
        Mirrors scripts/change_requests/paths.py, in the same order:
          1. an explicit -CrHome, or the CR_HOME environment variable
          2. the nearest ancestor holding a fa-web-app directory, which is the
             workspace root from a normal checkout AND from a worktree
          3. <repo>/change-requests as a last resort
    #>
    param([string] $Explicit, [string] $RepoRoot)

    if ($Explicit) { return [System.IO.Path]::GetFullPath($Explicit) }
    if ($env:CR_HOME) { return [System.IO.Path]::GetFullPath($env:CR_HOME) }

    $candidate = $RepoRoot
    while ($candidate) {
        if (Test-Path (Join-Path $candidate 'fa-web-app') -PathType Container) {
            return (Join-Path $candidate 'change-requests')
        }
        $parent = Split-Path -Parent $candidate
        if ($parent -eq $candidate) { break }
        $candidate = $parent
    }
    return (Join-Path $RepoRoot 'change-requests')
}

$crHome = Resolve-ChangeRequestHome -Explicit $CrHome -RepoRoot $repoRoot
$approvedDir = Join-Path $crHome 'approved'
$inboxDir = Join-Path $crHome 'inbox-msg'
$runsDir = Join-Path $crHome 'runs'

# --- 2. the common case: nothing to do, so do nothing ----------------------
# Note the asymmetry. A .msg in the inbox is worth a run because importing it
# is how it becomes something Jake can review. An empty approved/ on its own is
# not, because nothing else in this system can put a file there.
$hasApproved = (Test-Path $approvedDir) -and
    @(Get-ChildItem -Path $approvedDir -Filter 'CR-*.md' -File -ErrorAction SilentlyContinue).Count -gt 0
$hasInbox = (Test-Path $inboxDir) -and
    @(Get-ChildItem -Path $inboxDir -Filter '*.msg' -File -ErrorAction SilentlyContinue).Count -gt 0

if (-not $hasApproved -and -not $hasInbox) {
    exit 0
}

# --- 3. run it, and keep the transcript ------------------------------------
# extract_msg is a dev-only dependency and lives in the repo virtualenv, so
# prefer it. From a worktree there is no local .venv, so fall back to the main
# checkout's -- the workspace root is the parent of the data folder.
$venvCandidates = @(
    (Join-Path $repoRoot '.venv\Scripts\python.exe')
    (Join-Path (Split-Path -Parent $crHome) 'fa-web-api\.venv\Scripts\python.exe')
)
$python = 'python'
foreach ($candidate in $venvCandidates) {
    if (Test-Path $candidate) { $python = $candidate; break }
}

if (-not (Test-Path $runsDir)) {
    New-Item -ItemType Directory -Path $runsDir -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyy-MM-dd-HHmm'
$logPath = Join-Path $runsDir "scheduled-$stamp.log"

$commonArgs = @()
if ($CrHome -or $env:CR_HOME) { $commonArgs += @('--home', $crHome) }

$nextArgs = @('next', '--limit', "$Limit", '--repo', $Repo)
if ($Execute) { $nextArgs += '--execute' } else { $nextArgs += '--dry-run' }
if ($NoBranch) { $nextArgs += '--no-branch' }

$exitCode = 0

# Keep the CLI's em dashes readable in the log. The CLI reconfigures its own
# stdout to UTF-8; without this the console this script captures through would
# still be on the OEM code page.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'

function Write-Log {
    param([string[]] $Lines)
    $Lines | Out-File -FilePath $script:logPath -Encoding utf8 -Append
}

function Invoke-Cli {
    <# Run the CLI, show the output, log the output, return the exit code. #>
    param([string[]] $CliArgs)
    $output = & $python -m scripts.change_requests.cli @CliArgs 2>&1
    $code = $LASTEXITCODE
    $text = @($output | ForEach-Object { "$_" })
    if ($text.Count -gt 0) {
        # Write-Host, not Write-Output: this function RETURNS the exit code, and
        # anything written to the success stream would be returned alongside it.
        # `$code = Invoke-Cli ...` would then be an array, and every comparison
        # against 0 would quietly stop meaning what it says.
        $text | ForEach-Object { Write-Host $_ }
        Write-Log $text
    }
    return $code
}

Push-Location $repoRoot
try {
    @(
        "=== change-request scheduled run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
        "home:   $crHome"
        "mode:   $(if ($Execute) { 'execute' } else { 'dry-run' })"
        "python: $python"
        ''
    ) | Out-File -FilePath $logPath -Encoding utf8

    $code = Invoke-Cli -CliArgs ($commonArgs + @('import'))
    if ($code -ne 0) { $exitCode = $code }

    $code = Invoke-Cli -CliArgs ($commonArgs + $nextArgs)
    if ($code -ne 0) { $exitCode = $code }

    Write-Log @('', "exit:   $exitCode")
}
finally {
    Pop-Location
}

exit $exitCode
