@echo off
REM ============================================================================
REM  BPO Adriene - AI Voice Fronter  (one-click start, no PBX required)
REM  Double-click this file. It starts the neural voice server (:8880) and the
REM  app (:8000), then opens the dashboard in your browser.
REM
REM  For the live server (Asterisk/ARI) use:  start.ps1 -Serve
REM  Readiness report:                        start.ps1 -Check
REM ============================================================================
setlocal
cd /d "%~dp0"

set "PORT=8000"
set "VOICE_PORT=8880"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [setup] Creating virtual environment...
  py -3.12 -m venv .venv || python -m venv .venv
  if not exist "%PY%" (
    echo [error] Could not create a virtual environment. Install Python 3.12 x64.
    echo         See DEPLOYMENT.md for details.
    pause
    exit /b 1
  )
  echo [setup] Installing dependencies...
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [error] Dependency install failed. See DEPLOYMENT.md.
    pause
    exit /b 1
  )
)

if not exist ".env" (
  if exist ".env.example" (
    echo [setup] Creating .env from .env.example
    copy ".env.example" ".env" >nul
  )
)

if not exist "data\models\piper\*.onnx" (
  echo [setup] Downloading the neural voices ^(~190 MB, one time^)...
  "%PY%" scripts\fetch_neural_voices.py
)

if not exist "logs" mkdir "logs"

REM --- neural voice server (skip when the port is already taken) --------------
netstat -an | findstr /r /c:":%VOICE_PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo [voice] Starting the neural voice server on :%VOICE_PORT% ...
  start "BPO voice server (:8880)" /min "%PY%" scripts\neural_tts_server.py --host 0.0.0.0 --port %VOICE_PORT% --warm
  REM give it a moment to load the models
  timeout /t 6 /nobreak >nul
) else (
  echo [voice] A server is already listening on :%VOICE_PORT% ^(left as it is^)
)

echo.
echo ============================================================
echo   BPO Adriene - AI Voice Fronter
echo   Dashboard:      http://localhost:%PORT%/
echo   Campaigns:      http://localhost:%PORT%/campaigns
echo   Voice console:  http://localhost:%PORT%/debug
echo   Health:         http://localhost:%PORT%/health
echo   Voice server:   http://localhost:%VOICE_PORT%/health
echo   Press CTRL+C to stop the app (the voice server keeps its own window).
echo ============================================================
echo.

start "" "http://localhost:%PORT%/"

"%PY%" cli.py debug
if errorlevel 1 (
  echo.
  echo [info] Falling back to the system Python...
  python cli.py debug
)

endlocal
pause
