<#
.SYNOPSIS
  BPO Adriene - AI Voice Fronter launcher.

.DESCRIPTION
  Starts the server on http://localhost:9000 with the dashboard, the campaign
  editor and the live voice console. No PBX or phone system required.

.EXAMPLE
  .\start.ps1            # start the server
  .\start.ps1 -Port 8080 # start on a different port
  .\start.ps1 -Check     # run the readiness check only (no server)
#>
[CmdletBinding()]
param(
    [int]$Port = 9000,
    [switch]$Check,
    [switch]$Tests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- pick a Python -------------------------------------------------------
$python = $null
foreach ($candidate in @("$root\.venv\Scripts\python.exe", "python", "py")) {
    try {
        & $candidate -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) { $python = $candidate; break }
    } catch { }
}
if (-not $python) { Write-Host "[error] No Python interpreter found." -ForegroundColor Red; exit 1 }

Write-Host "[python] $python" -ForegroundColor DarkGray

# --- first-run bootstrap -------------------------------------------------
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "[setup] creating virtual environment..." -ForegroundColor Yellow
    & $python -m venv .venv
    & "$root\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    $python = "$root\.venv\Scripts\python.exe"
}
if ((Test-Path "$root\.env.example") -and -not (Test-Path "$root\.env")) {
    Write-Host "[setup] creating .env from .env.example" -ForegroundColor Yellow
    Copy-Item "$root\.env.example" "$root\.env"
}

$env:PORT = "$Port"

# --- optional modes ------------------------------------------------------
if ($Tests) {
    Write-Host "[tests] running the suite..." -ForegroundColor Cyan
    & $python -m pytest tests/ -q
    exit $LASTEXITCODE
}
if ($Check) {
    Write-Host "[check] running the readiness report..." -ForegroundColor Cyan
    & $python scripts/healthcheck.py
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  BPO Adriene - AI Voice Fronter"
Write-Host "  Dashboard:      http://localhost:$Port/"
Write-Host "  Campaigns:      http://localhost:$Port/campaigns"
Write-Host "  Voice console:  http://localhost:$Port/debug"
Write-Host "  Health:         http://localhost:$Port/health"
Write-Host "  Press CTRL+C to stop."
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

& $python cli.py debug
