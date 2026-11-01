# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Windows / PowerShell)
#
#    .\start.ps1              bootstrap if needed, then start on :8055
#    .\start.ps1 -Port 9000   another port
#    .\start.ps1 -Check       readiness report, then exit (no server)
#    .\start.ps1 -Tests       run the full test suite, then exit
#    .\start.ps1 -NoVoice     start with the browser voice (TTS_PROVIDER=mock)
#
#  One process serves the API, the WebSocket call flow, the Kokoro neural voice
#  engine and the built console. First start downloads the voice model
#  (~330 MB, ~40 s); it is cached after that.
#
#  SECRET_KEY is REQUIRED — see .env.example. This launcher will not invent a
#  default secret, because a shipped default secret is a shipped vulnerability.
# =============================================================================
param(
    [int]$Port = 8055,
    [string]$Python = "python",
    [switch]$Tests,
    [switch]$Check,
    [switch]$NoVoice
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Venv = ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

# --- 1. .env: create from the example on first run, never overwrite --------
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example" -ForegroundColor Cyan
    }
}

# --- 2. dependencies (skipped in test mode until we know the interpreter) --
function Install-Deps($py) {
    if (-not (Test-Path $VenvPython)) {
        Write-Host "Creating virtualenv in $Venv (first run only)..." -ForegroundColor Cyan
        & $py -m venv $Venv
    }
    & $VenvPython -c "import fastapi" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing dependencies (first run only)..." -ForegroundColor Cyan
        & $VenvPython -m pip install --upgrade pip | Out-Null
        & $VenvPython -m pip install -r requirements.txt
    }
    return $VenvPython
}

if ($Tests) {
    $py = if (Test-Path $VenvPython) { $VenvPython } else { $Python }
    if (-not (Test-Path $VenvPython)) { $py = Install-Deps $Python }
    & $py -m pytest tests/ backend/tests/
    exit $LASTEXITCODE
}

$py = Install-Deps $Python

# --- 3. SECRET_KEY is required: fail fast, never default ------------------
if (-not $env:SECRET_KEY) {
    Write-Host ""
    Write-Host "ERROR: SECRET_KEY is not set." -ForegroundColor Red
    Write-Host "Refusing to start with a default secret — anyone who knows the"
    Write-Host "default can forge dashboard sessions. Generate one with:"
    Write-Host ""
    Write-Host '    python -c "import secrets; print(secrets.token_urlsafe(32))"'
    Write-Host ""
    Write-Host "then add  SECRET_KEY=<that value>  to .env"
    Write-Host ""
    exit 1
}

if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "sqlite:///./dev.db" }
$env:HF_HUB_DISABLE_XET = "1"   # xet chunk-downloader can fill the disk — keep it off
if ($NoVoice) { $env:TTS_PROVIDER = "mock" }

# --- 4. readiness check ---------------------------------------------------
if ($Check) {
    & $py scripts/healthcheck.py
    exit $LASTEXITCODE
}

# --- 5. build the console once -------------------------------------------
if (-not (Test-Path "frontend\build\index.html")) {
    Write-Host "Building the console (first run only)..." -ForegroundColor Cyan
    Push-Location frontend
    if (-not (Test-Path "node_modules")) { npm install }
    npm run build
    Pop-Location
}

Write-Host "Adriene on http://localhost:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& $py -m uvicorn backend.app.main:app --port $Port
