@echo off
REM ===========================================================================
REM  BPO Adriene - AI Voice Bot launcher (Windows, cmd)
REM
REM    start.bat             bootstrap if needed, then open http://localhost:8055
REM    start.bat --tests     run the full test suite, then exit
REM
REM  SECRET_KEY is REQUIRED - see .env.example. This launcher will not invent a
REM  default secret, because a shipped default secret is a shipped vulnerability.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM --- 1. .env: create from the example on first run, never overwrite --------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo Created .env from .env.example
    )
)

REM --- 2. virtualenv + dependencies -----------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtualenv in .venv ^(first run only^)...
    python -m venv .venv || goto :novenv
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import fastapi" 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(first run only^)...
    "%PY%" -m pip install --upgrade pip >nul
    "%PY%" -m pip install -r requirements.txt || goto :noinstall
)

if /i "%~1"=="--tests" (
    "%PY%" -m pytest tests/ backend/tests/
    exit /b %errorlevel%
)

REM --- 3. SECRET_KEY is required: fail fast, never default ------------------
if "%SECRET_KEY%"=="" (
    echo.
    echo ERROR: SECRET_KEY is not set.
    echo Refusing to start with a default secret - anyone who knows the default
    echo can forge dashboard sessions. Generate one and put it in .env:
    echo.
    echo     python -c "import secrets; print(secrets.token_urlsafe(32))"
    echo.
    pause
    exit /b 1
)

if "%DATABASE_URL%"=="" set DATABASE_URL=sqlite:///./dev.db
set HF_HUB_DISABLE_XET=1

REM --- 4. build the console once -------------------------------------------
if not exist "frontend\build\index.html" (
    echo Building the console ^(first run only^)...
    pushd frontend
    if not exist "node_modules" call npm install
    call npm run build
    popd
)

echo Adriene on http://localhost:8055  ^(Ctrl+C to stop^)
"%PY%" -m uvicorn backend.app.main:app --port 8055
exit /b %errorlevel%

:novenv
echo ERROR: could not create the virtualenv. Is Python 3.10+ on PATH?
pause
exit /b 1

:noinstall
echo ERROR: dependency install failed. See the output above.
pause
exit /b 1
