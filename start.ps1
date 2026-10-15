<#
.SYNOPSIS
  BPO Adriene — AI Voice Fronter launcher (dashboard + campaigns + voice studio).

.DESCRIPTION
  Starts everything a working deployment needs:

    1. the local neural voice server on :8880   (Piper/ONNX, no key, no torch)
    2. the app on :8000                         (dashboard, console, campaigns)

  In debug mode no PBX is required: the console runs a scripted demo call and the
  dashboard's "Test voice" plays real audio. Use -Serve on the live server, where
  Asterisk/ARI and the campaign gates run for real.

.PARAMETER Port
  TCP port for the app (default 8000; PORT in .env is overridden by this).

.PARAMETER Serve
  Production mode: `cli.py serve` — ARI listener + web, no demo call, DEBUG off.

.PARAMETER Check
  Run the readiness report (`scripts/healthcheck.py`) and exit.

.PARAMETER Tests
  Run the test suite (`pytest tests/ -q`) and exit.

.PARAMETER NoVoice
  Do not start the neural voice server (use when another engine is configured,
  e.g. ElevenLabs, or when it is already running as a Windows service).

.PARAMETER VoicePort
  Port for the neural voice server (default 8880 — keep PIPER_BASE_URL in .env
  in sync when you change it).

.EXAMPLE
  .\start.ps1                 # debug: dashboard + demo call + local voices
  .\start.ps1 -Serve          # live server: ARI listener + web
  .\start.ps1 -Check          # readiness report only
  .\start.ps1 -Port 8080 -VoicePort 8881
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$VoicePort = 8880,
    [switch]$Serve,
    [switch]$Check,
    [switch]$Tests,
    [switch]$NoVoice
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Ok   ($m) { Write-Host $m -ForegroundColor Green }
function Write-Info ($m) { Write-Host $m -ForegroundColor Cyan }
function Write-Warn ($m) { Write-Host $m -ForegroundColor Yellow }
function Write-Err  ($m) { Write-Host $m -ForegroundColor Red }

function Test-PortOpen([int]$Port, [int]$TimeoutMs = 700) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch { return $false } finally { $client.Close() }
}

# --- pick a Python ---------------------------------------------------------
$python = $null
foreach ($candidate in @("$root\.venv\Scripts\python.exe", "py", "python")) {
    try {
        & $candidate -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Write-Err "[error] No Python interpreter found. Install Python 3.12 x64, then re-run."
    exit 1
}

# --- first-run bootstrap ---------------------------------------------------
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Warn "[setup] First run: creating .venv and installing dependencies..."
    & $python -m venv .venv
    if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
        Write-Err "[error] Could not create .venv. Install Python 3.12 x64. See DEPLOYMENT.md."
        exit 1
    }
    & "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & "$root\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    $python = "$root\.venv\Scripts\python.exe"
}
if (Test-Path "$root\.env.example") {
    if (-not (Test-Path "$root\.env")) {
        Copy-Item "$root\.env.example" "$root\.env"
        Write-Warn "[setup] Created .env from .env.example — edit it (keys, ARI, WEB_PASSWORD)."
    }
}

if ($Tests) {
    Write-Info "[tests] running the suite..."
    & $python -m pytest tests/ -q
    exit $LASTEXITCODE
}
if ($Check) {
    Write-Info "[check] running the readiness report..."
    & $python scripts/healthcheck.py --url "http://localhost:$Port"
    exit $LASTEXITCODE
}

$env:PORT = "$Port"
$env:PIPER_BASE_URL = "http://127.0.0.1:$VoicePort"

New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

# --- 1. local neural voice server -----------------------------------------
$voiceProc = $null
if (-not $NoVoice) {
    if (Test-PortOpen $VoicePort) {
        Write-Ok "[voice] already running on :$VoicePort (left as it is)"
    } else {
        $voiceLog = "$root\logs\voice-server.log"
        Write-Info "[voice] starting the neural voice server on :$VoicePort (log: logs\voice-server.log)"
        $voiceProc = Start-Process -FilePath $python `
            -ArgumentList @("scripts\neural_tts_server.py", "--host", "0.0.0.0",
                            "--port", "$VoicePort", "--warm") `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $voiceLog -RedirectStandardError "$root\logs\voice-server.err.log"

        $ready = $false
        foreach ($i in 1..40) {
            Start-Sleep -Milliseconds 500
            if (Test-PortOpen $VoicePort) { $ready = $true; break }
        }
        if ($ready) {
            Write-Ok "[voice] ready on :$VoicePort"
        } else {
            Write-Warn "[voice] not answering yet — check logs\voice-server.log"
            Write-Warn "        (the dashboard still works; audio needs the voice server)"
        }
    }
} else {
    Write-Warn "[voice] skipped (-NoVoice)"
}

# --- 2. the app ------------------------------------------------------------
$mode = if ($Serve) { "serve (live: ARI + web)" } else { "debug (demo call + web)" }
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  BPO Adriene - AI Voice Fronter          [$mode]"
Write-Host "  Dashboard:      http://localhost:$Port/"
Write-Host "  Campaigns:      http://localhost:$Port/campaigns"
Write-Host "  Voice console:  http://localhost:$Port/debug"
Write-Host "  Voice studio:   http://localhost:$Port/  (Voice studio panel)"
Write-Host "  Health:         http://localhost:$Port/health"
Write-Host "  Voice server:   http://localhost:$VoicePort/health"
Write-Host "  Press CTRL+C to stop."
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

try {
    if ($Serve) { & $python cli.py serve } else { & $python cli.py debug }
} finally {
    if ($voiceProc -and -not $voiceProc.HasExited) {
        Write-Info "[voice] stopping the neural voice server (pid $($voiceProc.Id))"
        try { $voiceProc.Kill() } catch { }
    }
}
