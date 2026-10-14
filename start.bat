@echo off
REM =========================================================================
REM  BPO Adriene - AI Voice Fronter  (one-click start, no PBX required)
REM  Double-click this file. It opens http://localhost:9000
REM =========================================================================
setlocal
cd /d "%~dp0"

set "PORT=9000"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating virtual environment...
  py -3.12 -m venv .venv || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [error] Could not create a virtual environment. Install Python 3.12.
    pause
    exit /b 1
  )
  echo [setup] Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist ".env" (
  if exist ".env.example" (
    echo [setup] Creating .env from .env.example
    copy ".env.example" ".env" >nul
  )
)

echo.
echo ============================================================
echo   BPO Adriene - AI Voice Fronter
echo   Dashboard:      http://localhost:%PORT%/
echo   Campaigns:      http://localhost:%PORT%/campaigns
echo   Voice console:  http://localhost:%PORT%/debug
echo   Health:         http://localhost:%PORT%/health
echo   Press CTRL+C to stop.
echo ============================================================
echo.

".venv\Scripts\python.exe" cli.py debug
if errorlevel 1 (
  echo.
  echo [info] Falling back to the system Python...
  python cli.py debug
)

endlocal
pause
