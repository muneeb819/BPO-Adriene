# ============================================================================
# start-kokoro.ps1 -- install & run the local Kokoro TTS server
#
# Kokoro is the FREE, self-hosted TTS engine used as the bot's standby voice.
# ElevenLabs stays primary; if it ever fails mid-call the bot switches to
# Kokoro automatically (and back again when ElevenLabs recovers).
#
# The bot talks to it over HTTP, exactly like the OpenAI audio API:
#     POST http://localhost:8880/v1/audio/speech   -> raw PCM @ 24 kHz
#     GET  http://localhost:8880/v1/audio/voices
#
# USAGE
#   .\start-kokoro.ps1                 install (if needed) + start the server
#   .\start-kokoro.ps1 -InstallOnly   install only, do not start
#   .\start-kokoro.ps1 -Path D:\kokoro use a different Kokoro source folder
#   .\start-kokoro.ps1 -Stop          stop a running server
#
# First run downloads ~1.5-2 GB (torch CPU + model weights). Make sure you
# have at least 4 GB free on the drive holding the uv cache.
# ============================================================================
[CmdletBinding()]
param(
    [string]$Path = "",
    [switch]$InstallOnly,
    [switch]$Stop,
    [int]$Port = 8880
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- locate the Kokoro source ------------------------------------------------
$candidates = @(
    $Path,
    (Join-Path $ProjectRoot "Kokoro-FastAPI-master"),
    (Join-Path $ProjectRoot "..\Kokoro-FastAPI-master"),
    "C:\Users\Wave\Downloads\Kokoro-FastAPI-master"
) | Where-Object { $_ }

$kokoro = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path (Join-Path $c "api\src\main.py"))) { $kokoro = (Resolve-Path $c).Path; break }
}

if (-not $kokoro) {
    $zip = "C:\Users\Wave\Downloads\Kokoro-FastAPI-master.zip"
    if (Test-Path $zip) {
        $dest = Join-Path $ProjectRoot "Kokoro-FastAPI-master"
        Write-Host "Extracting Kokoro source from $zip ..." -ForegroundColor Cyan
        Expand-Archive -Path $zip -DestinationPath $ProjectRoot -Force
        $kokoro = $dest
    }
}

if (-not $kokoro) {
    Write-Host "Kokoro source not found. Either:" -ForegroundColor Red
    Write-Host "  - pass -Path <folder> pointing at a Kokoro-FastAPI checkout," -ForegroundColor Red
    Write-Host "  - place Kokoro-FastAPI-master.zip next to this script, or" -ForegroundColor Red
    Write-Host "  - clone it:  git clone https://github.com/hexgrad/Kokoro-FastAPI" -ForegroundColor Red
    exit 1
}
Write-Host "Kokoro source: $kokoro" -ForegroundColor Green

# --- prerequisites ----------------------------------------------------------
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "uv is not installed. Get it from https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

$freeGb = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($kokoro[0]):'").FreeSpace / 1GB
if ($freeGb -lt 4) {
    Write-Host ("`n  WARNING: only {0:N1} GB free on drive {1}:." -f $freeGb, $kokoro[0]) -ForegroundColor Yellow
    Write-Host "  The CPU install needs roughly 1.5-2 GB plus the uv cache." -ForegroundColor Yellow
    Write-Host "  Free up space or point -Path at a drive with more room.`n" -ForegroundColor Yellow
}

# eSpeak NG drives the phonemizer. espeakng-loader ships a copy, but pointing
# at a real install is more reliable.
$espeak = "C:\Program Files\eSpeak NG\libespeak-ng.dll"
if (Test-Path $espeak) {
    $env:PHONEMIZER_ESPEAK_LIBRARY = $espeak
    Write-Host "eSpeak NG: $espeak" -ForegroundColor Green
} else {
    Write-Host "eSpeak NG not found at $espeak - will rely on the bundled espeakng-loader." -ForegroundColor Yellow
}

# --- stop mode --------------------------------------------------------------
if ($Stop) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Stopped Kokoro on port $Port" -ForegroundColor Green
    } else {
        Write-Host "Nothing listening on port $Port" -ForegroundColor DarkGray
    }
    exit 0
}

# --- install ----------------------------------------------------------------
# Split into two phases so re-running is cheap: the dependency install only
# happens when the venv is missing torch, and the model download only when the
# weights are absent.
$venvDir   = Join-Path $kokoro ".venv"
$modelDir  = Join-Path $kokoro "api\src\models\v1_0"
$modelFile = Join-Path $modelDir "kokoro-v1_0.pth"

$env:PYTHONUTF8 = "1"
$env:PROJECT_ROOT = $kokoro
$env:USE_GPU = "false"
$env:PYTHONPATH = "$kokoro;$kokoro/api"
$env:MODEL_DIR = "src/models"
$env:VOICES_DIR = "src/voices/v1_0"
$env:WEB_PLAYER_PATH = Join-Path $kokoro "web"

$hasTorch = $false
if (Test-Path $venvDir) {
    Push-Location $kokoro
    try {
        uv run --no-sync python -c "import torch" *> $null
        $hasTorch = ($LASTEXITCODE -eq 0)
    } finally {
        Pop-Location
    }
}

if (-not $hasTorch) {
    Write-Host "Installing Kokoro (CPU). This downloads torch + weights -- expect a few minutes." -ForegroundColor Cyan
    Push-Location $kokoro
    try {
        if (-not (Test-Path $venvDir)) {
            Write-Host "Creating virtual environment..."
            uv venv --python 3.12
            if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
        }
        uv pip install -e ".[cpu]"
        if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }
    } finally {
        Pop-Location
    }
    Write-Host "Dependencies installed." -ForegroundColor Green
} else {
    Write-Host "Dependencies already present - skipping." -ForegroundColor DarkGray
}

if (-not (Test-Path $modelFile)) {
    Write-Host "Downloading the Kokoro v1.0 model weights..."
    Push-Location $kokoro
    try {
        uv run --no-sync python docker\scripts\download_model.py --output $modelDir
        if ($LASTEXITCODE -ne 0) { throw "model download failed" }
    } finally {
        Pop-Location
    }
    Write-Host "Model ready." -ForegroundColor Green
} else {
    Write-Host "Model already present - skipping." -ForegroundColor DarkGray
}
# --- start ------------------------------------------------------------------
$env:PYTHONUTF8 = "1"
$env:PROJECT_ROOT = $kokoro
$env:USE_GPU = "false"
$env:PYTHONPATH = "$kokoro;$kokoro\api"
$env:MODEL_DIR = "src\models"
$env:VOICES_DIR = "src\voices\v1_0"
$env:WEB_PLAYER_PATH = Join-Path $kokoro "web"

Write-Host "Starting Kokoro on http://localhost:$Port (Ctrl+C to stop)" -ForegroundColor Cyan
Push-Location $kokoro
try {
    uv run --no-sync uvicorn api.src.main:app --host 0.0.0.0 --port $Port
} finally {
    Pop-Location
}
