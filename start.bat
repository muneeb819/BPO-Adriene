@echo off
REM =========================================================================
REM  BPO Adriene - AI Voice Bot (one-click start, Windows)
REM  Double-click this file, then open https://localhost:8055
REM  (browser will warn about the self-signed dev cert - click Advanced ->
REM   Proceed to localhost; this is expected for local development)
REM =========================================================================
cd /d "%~dp0"
if "%SECRET_KEY%"=="" set SECRET_KEY=dev-secret
if "%DATABASE_URL%"=="" set DATABASE_URL=sqlite:///./dev.db
set HF_HUB_DISABLE_XET=1
if not exist "backend\certs\cert.pem" (
  python backend\scripts\gen_cert.py
)
python -m uvicorn backend.app.main:app --port 8055 --ssl-keyfile backend/certs/key.pem --ssl-certfile backend/certs/cert.pem
pause