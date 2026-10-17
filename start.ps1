# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Windows)
#
#    .\\start.ps1              start the bot on https://localhost:8055
#    .\\start.ps1 -Port 9000   start on another port
#
#  HTTPS is on by default (self-signed dev cert for localhost; your browser will
#  show a one-time "not private" warning — click Advanced -> Proceed). The
#  frontend switches to wss:// automatically. Real certs: drop cert.pem + key.pem
#  into backend\\certs\\ (or terminate TLS at a reverse proxy in production).
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

if (-not (Test-Path "frontend\\build\\index.html")) {
    Write-Host "Building the console (first run only)..." -ForegroundColor Cyan
    Push-Location frontend
    if (-not (Test-Path "node_modules")) { npm install }
    npm run build
    Pop-Location
}

if (-not (Test-Path "backend\\certs\\cert.pem")) {
    Write-Host "Generating self-signed TLS cert (first run)..." -ForegroundColor Cyan
    & $Python backend\\scripts\\gen_cert.py
}

Write-Host "Adriene on https://localhost:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $Python -m uvicorn backend.app.main:app --port $Port `
    --ssl-keyfile backend/certs/key.pem --ssl-certfile backend/certs/cert.pem