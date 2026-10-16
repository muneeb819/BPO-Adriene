# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Windows)
#
#    .\start.ps1              start the bot on http://localhost:8055
#    .\start.ps1 -Port 9000   start on another port
#
#  The backend serves the API, the WebSocket call flow, the Kokoro neural
#  voice engine and the built console — all on one port. First start downloads
#  the voice model (~330 MB, ~40 s); it is cached after that.
# =============================================================================
param(
    [int]$Port = 8055,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $env:SECRET_KEY)     { $env:SECRET_KEY = "dev-secret" }
if (-not $env:DATABASE_URL)   { $env:DATABASE_URL = "sqlite:///./dev.db" }
$env:HF_HUB_DISABLE_XET = "1"   # xet chunk-downloader can fill the disk — keep it off

if (-not (Test-Path "frontend\build\index.html")) {
    Write-Host "Building the console (first run only)..." -ForegroundColor Cyan
    Push-Location frontend
    if (-not (Test-Path "node_modules")) { npm install }
    npm run build
    Pop-Location
}

Write-Host "Adriene on http://localhost:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $Python -m uvicorn backend.app.main:app --port $Port
