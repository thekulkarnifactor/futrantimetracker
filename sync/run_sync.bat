@echo off
setlocal
title greytHR Sync Agent
cd /d "%~dp0"

rem ---- one-time setup: virtual env + dependencies + Chromium -------------
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    ".venv\Scripts\python.exe" -m playwright install chromium || goto :fail
)

if not exist ".env" (
    if "%SUPABASE_SERVICE_KEY%"=="" (
        echo [error] No .env file and SUPABASE_SERVICE_KEY is not set.
        echo         Copy .env.example to .env and paste the service_role key.
        pause
        exit /b 1
    )
)

rem ---- run forever, restart on crash with a growing delay ----------------
set DELAY=10
:loop
echo [%date% %time%] Starting sync agent...
".venv\Scripts\python.exe" greyt_sync.py
set CODE=%ERRORLEVEL%
if "%CODE%"=="0" (
    echo [%date% %time%] Agent stopped cleanly.
    goto :eof
)
echo [%date% %time%] Agent exited with code %CODE%. Restarting in %DELAY%s...
timeout /t %DELAY% /nobreak >nul
set /a DELAY=DELAY*2
if %DELAY% GTR 300 set DELAY=300
goto loop

:fail
echo [error] Setup failed. Check Python 3.10+ is installed and on PATH.
pause
exit /b 1
