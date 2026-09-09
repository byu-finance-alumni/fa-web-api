<#
.SYNOPSIS
    Thin wrapper around the change-request intake CLI.

.DESCRIPTION
    So the command is `.\scripts\request.ps1 import` instead of
    `python -m scripts.change_requests.cli import`. Every argument is passed
    straight through.

    Run it from the repo root. It prefers the repo's virtualenv if one exists,
    because `extract_msg` is a dev-only dependency and is usually installed
    there rather than system-wide.

.EXAMPLE
    .\scripts\request.ps1 setup
    .\scripts\request.ps1 import
    .\scripts\request.ps1 validate CR-2026-001
    .\scripts\request.ps1 start CR-2026-001 --repo fa-web-api
#>

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

Push-Location $repoRoot
try {
    & $python -m scripts.change_requests.cli @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
