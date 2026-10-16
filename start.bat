@echo off
REM =========================================================================
REM  BPO Adriene - AI Voice Bot (one-click start, Windows)
REM  Double-click this file, then open http://localhost:8055
REM =========================================================================
cd /d "%~dp0"
if "%SECRET_KEY%"=="" set SECRET_KEY=dev-secret
if "%DATABASE_URL%"=="" set DATABASE_URL=sqlite:///./dev.db
set HF_HUB_DISABLE_XET=1
python -m uvicorn backend.app.main:app --port 8055
pause
