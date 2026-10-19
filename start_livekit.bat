@echo off
REM =========================================================================
REM  BPO Adriene - LiveKit launcher (Windows, one click)
REM  Double-click to run the agent in debug mode.
REM  Args: dev (default) ^| start ^| console ^| doctor
REM =========================================================================
setlocal
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=dev"

set "POWERSHELL=powershell"
where pwsh >nul 2>&1 && set "POWERSHELL=pwsh"

%POWERSHELL% -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_livekit.ps1" -Mode %MODE%
if errorlevel 1 pause
