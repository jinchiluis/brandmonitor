@echo off
rem Manual repair after a title gate that did not judge a news run.
rem
rem The title gate judges only the items first stored by the latest news run, so a
rem run whose gate exited 2 (rejected API key, unknown model) or crashed is never
rem judged by a later run - its title-only headlines get no body fetch and never
rem reach the alert gate. Transient API errors do not need this: those batches
rem are kept whole. Fix the cause first (usually .env), then:
rem
rem   tools\repair_title_gate.bat              list the last ten news runs
rem   tools\repair_title_gate.bat 61 62        re-gate runs 61 and 62
rem
rem The run id is printed by the gate stage in run_daily.txt / run_intraday.txt
rem ("title gate: jt-express, collection run 61 ..."). After the gates it fetches
rem the kept bodies and runs the alert gate, so a repaired keep can alert now
rem rather than at the next scheduled run. A run gated before the crash is judged
rem again in full; the later decision wins, so the cost is only the extra calls.
rem
rem Holds data\run.lock like run_daily.bat and run_intraday.bat, so none overlap.

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [repair] interpreter not found: %PY%
    exit /b 2
)

if "%~1"=="" (
    echo Last ten news collection runs - id, started UTC, note:
    "%PY%" -c "import sqlite3;c=sqlite3.connect('file:data/brandmonitor.sqlite3?mode=ro',uri=True);[print(*r) for r in c.execute('SELECT id, started_at, note FROM run WHERE kind=? ORDER BY id DESC LIMIT 10',('news',))]"
    echo.
    echo usage: tools\repair_title_gate.bat ^<run id^> [^<run id^> ...]
    exit /b 0
)

set "CODE=3"
2>nul (
    9>"%CD%\data\run.lock" (
        call :repair %*
        set "CODE=!ERRORLEVEL!"
    )
) || (
    echo [repair] another run holds data\run.lock - wait for it to finish
    exit /b 3
)
exit /b !CODE!


:repair
set "WORST=0"
for %%R in (%*) do (
    echo.
    echo [repair] title gate for news run %%R
    "%PY%" run.py gate --run %%R
    if errorlevel 2 (
        echo [repair] gate for run %%R exited 2 - cause not fixed, stopping
        exit /b 2
    )
    if errorlevel 1 set "WORST=1"
)
echo.
echo [repair] fetching kept bodies
"%PY%" run.py fetch-bodies --kind news --title-gate-client jt-express
if errorlevel 2 exit /b 2
echo.
echo [repair] alert gate
"%PY%" run.py alert-gate
if errorlevel 2 exit /b 2
exit /b %WORST%
