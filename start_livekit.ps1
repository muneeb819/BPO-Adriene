# =============================================================================
#  BPO Adriene — LiveKit launcher (Windows PowerShell)
#
#    .\start_livekit.ps1                debug/dev mode (recommended)
#    .\start_livekit.ps1 -Mode start    production worker
#    .\start_livekit.ps1 -Mode console  local terminal, no LiveKit server
#    .\start_livekit.ps1 -Mode doctor   check config + reachability, then exit
#
#  Reads credentials from .\.env (LIVEKIT_URL / LIVEKIT_API_KEY /
#  LIVEKIT_API_SECRET). On first run it creates .venv, installs
#  requirements-livekit.txt and fetches a local neural voice.
# =============================================================================
param(
    [ValidateSet("dev", "start", "console", "doctor")]
    [string]$Mode = "dev",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-PythonImport {
    param([string]$Interpreter, [string]$Code)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Interpreter -c $Code *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $previous
    return $ok
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating .venv (first run)..." -ForegroundColor Cyan
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "could not create the virtual environment - install Python 3.11+ and put it on PATH"
    }
}

$py = $venvPy

if (-not (Test-PythonImport $py "import livekit.agents")) {
    Write-Host "Installing the LiveKit runtime (first run, a few minutes)..." -ForegroundColor Cyan
    & $py -m pip install -q -U pip setuptools wheel
    & $py -m pip install -q -r requirements-livekit.txt
}

if (-not (Test-PythonImport $py "import fastapi, uvicorn")) {
    & $py -m pip install -q fastapi "uvicorn[standard]" sqlmodel python-dotenv
}

# A local voice is what makes the bot audible with zero cloud keys.
$voices = @(Get-ChildItem "data\models\piper\*.onnx" -ErrorAction SilentlyContinue)
if ($voices.Count -eq 0) {
    Write-Host "Fetching a local neural voice (~60 MB, once)..." -ForegroundColor Cyan
    & $py scripts/fetch_neural_voices.py --voice en_US-amy-low
}

if ($Mode -eq "doctor") {
    & $py scripts/livekit_doctor.py
    exit $LASTEXITCODE
}

if (-not $env:LOG_LEVEL) { $env:LOG_LEVEL = "DEBUG" }
Write-Host "Adriene -> LiveKit ($Mode mode). Ctrl+C to stop." -ForegroundColor Green
& $py livekit_agent.py $Mode --log-level debug
