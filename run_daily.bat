@echo off
rem brandmonitor daily collection.
rem
rem News, regulatory articles and Safety Gate, plus the title gate over the news
rem items the day's crawl stored for the first time and the body gate over every
rem selected body that has no decision yet. Fetch-on-match, the full assessment and
rem the weekly report are not wired in yet and are deliberately absent rather than
rem stubbed.
rem
rem Stages never chain on success: collection is source-specific and the two source
rem lists are independent, so a news failure must not cancel regulatory collection.
rem Each stage records its own code and the run reports the worst one.
rem
rem   0  every stage completed
rem   1  a stage produced nothing usable (every source in it failed)
rem   2  a stage aborted, or this script could not start one
rem   3  another run holds the lock; nothing was attempted

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [brandmonitor] interpreter not found: %PY%
    echo [brandmonitor] create it with: python -m venv .venv ^&^& .venv\Scripts\python -m pip install -r requirements.txt
    exit /b 2
)

if not exist "%~dp0data" mkdir "%~dp0data"
set "TMPVAL=%~dp0data\.runval"

rem Ask Python for the date: %%DATE%% is locale-dependent, and this fails early if
rem the interpreter is broken rather than half way through a stage. `for /f` is not
rem used here - it re-parses the command and strips the quotes around `-c`.
"%PY%" -c "import datetime;print(datetime.date.today())" > "%TMPVAL%" 2>"%TMPVAL%.err"
if errorlevel 1 (
    echo [brandmonitor] interpreter did not run: %PY%
    type "%TMPVAL%.err"
    exit /b 2
)
set /p DAY=<"%TMPVAL%"
rem A warning printed before the date would silently become the log directory name.
echo %DAY%| findstr /r /c:"^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$" >nul
if errorlevel 1 (
    echo [brandmonitor] unexpected date from interpreter: "%DAY%"
    exit /b 2
)

set "LOGDIR=%~dp0data\log\%DAY%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "OUT=%LOGDIR%\run_daily.txt"

set "WORST=0"
set "CODE_news=2"
set "CODE_title_gate=2"
set "CODE_regulatory=2"
set "CODE_safety_gate=2"
set "CODE_body_gate=2"
set "CODE_backup=2"

rem One SQLite writer at a time. A daily run must not collide with a manual
rem backfill; the handle on the lock file is held for as long as the block runs.
2>nul (
    9>"%~dp0data\run.lock" ( call :stages )
) || (
    echo [brandmonitor] another run holds data\run.lock - nothing attempted
    exit /b 3
)

"%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
set /p FINISHED=<"%TMPVAL%"
del "%TMPVAL%" "%TMPVAL%.err" 2>nul
> "%~dp0data\last_run.json" (
    echo {
    echo   "finished_utc": "!FINISHED!Z",
    echo   "worst_exit": !WORST!,
    echo   "stages": { "news": !CODE_news!, "title_gate": !CODE_title_gate!, "regulatory": !CODE_regulatory!, "safety_gate": !CODE_safety_gate!, "body_gate": !CODE_body_gate!, "backup": !CODE_backup! },
    echo   "log": "data/log/%DAY%/run_daily.txt"
    echo }
)

echo [brandmonitor] done, worst exit=!WORST!  log: %OUT%
exit /b %WORST%


:stages
rem Always returns 0 so the lock block above can tell "did not start" from "ran
rem badly"; the outcome travels in WORST.
echo.>> "%OUT%"
echo ======== brandmonitor daily %DATE% %TIME% ========>> "%OUT%"
call :stage news collect --kind news
rem Not chained on news succeeding: a crawl that aborted half way still stored
rem items worth gating, and when no crawl ran today the gate finds its run too old
rem and does nothing rather than repeating yesterday.
call :stage title_gate gate
call :stage regulatory collect --kind regulatory
call :stage safety_gate collect-safety-gate
rem After both collections, so the day's news and regulatory bodies are stored.
rem It reads every selected body without a decision, not only today's, so a body
rem that arrived on a retry is gated whenever it arrives; config.json's limit caps
rem each tier per run, so the first run after a backfill drains it over a few days.
call :stage body_gate body-gate
rem Backup runs last so the snapshot carries the day's collection rather than
rem yesterday's. It is safe inside the lock: SQLite's online backup API copies a
rem consistent snapshot while the database is open, and this stage only reads.
call :stage backup backup
exit /b 0


:stage
rem %1 stage name, %2.. arguments for run.py
echo.>> "%OUT%"
echo -------- %1 -------->> "%OUT%"
"%PY%" run.py %2 %3 %4 %5 %6 %7 %8 %9 >> "%OUT%" 2>&1
set "CODE=!ERRORLEVEL!"
set "CODE_%1=!CODE!"
echo [%1] exit=!CODE!>> "%OUT%"
echo [brandmonitor] %1 exit=!CODE!
if !CODE! GTR !WORST! set "WORST=!CODE!"
exit /b 0
