@echo off
rem Manual operator wrapper for tools\backfill_funnel.py.
rem Holds the same file handle as run_daily.bat, so neither can overlap the other.

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [backfill] interpreter not found: %PY%
    exit /b 2
)
if not exist "%CD%\data" mkdir "%CD%\data"

set "BRANDMONITOR_BACKFILL_LOCKED=1"
set "CODE=3"
2>nul (
    9>"%CD%\data\run.lock" (
        "%PY%" tools\backfill_funnel.py %*
        set "CODE=!ERRORLEVEL!"
    )
) || (
    echo [backfill] another run holds data\run.lock - nothing attempted
    exit /b 3
)
exit /b !CODE!
