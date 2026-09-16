@echo off
rem brandmonitor intraday news pass.
rem
rem Repeats only the news path of run_daily.bat - collection, title gate, body fetch,
rem news body gate and the alert gate - so a potential alert reaches the reviewer
rem the same day instead of after the next 06:00 run. Scheduled every two hours
rem from 08:00 to 22:00 as brandmonitor-intraday; 23:00-05:00 is left free for
rem Windows updates and restarts.
rem
rem It is optional. Collection windows chain from each source's watermark, so the
rem 06:00 daily run simply picks up where the last intraday run stopped, and it
rem remains the complete record: regulatory sources, parliamentary procedures,
rem backup and the health observers run only there.
rem
rem   0  every stage completed
rem   1  a stage produced nothing usable
rem   2  a stage aborted, or this script could not start one
rem   3  another run holds the lock; nothing was attempted and no marker is written
rem   4  this host has no internet; nothing was attempted

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [brandmonitor] interpreter not found: %PY%
    exit /b 2
)

if not exist "%~dp0data" mkdir "%~dp0data"
rem Its own scratch file: run_daily.bat deletes data\.runval when it finishes.
set "TMPVAL=%~dp0data\.runval-intraday"

rem Python supplies the date for the same reasons as in run_daily.bat.
"%PY%" -c "import datetime;print(datetime.date.today())" > "%TMPVAL%" 2>"%TMPVAL%.err"
if errorlevel 1 (
    echo [brandmonitor] interpreter did not run: %PY%
    type "%TMPVAL%.err"
    exit /b 2
)
set /p DAY=<"%TMPVAL%"
echo %DAY%| findstr /r /c:"^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$" >nul
if errorlevel 1 (
    echo [brandmonitor] unexpected date from interpreter: "%DAY%"
    exit /b 2
)

set "LOGDIR=%~dp0data\log\%DAY%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "OUT=%LOGDIR%\run_intraday.txt"

echo.>> "%OUT%"
echo ======== brandmonitor intraday %DATE% %TIME% ========>> "%OUT%"

rem Preflight, for the reasons in run_daily.bat and src/net.py. This slot does not
rem wait for the network: seven more follow it, and a waiter would hold the
rem run lock, which would make the next one exit 3 instead of running.
echo.>> "%OUT%"
echo -------- netcheck -------->> "%OUT%"
"%PY%" run.py netcheck >> "%OUT%" 2>&1
if errorlevel 4 (
    "%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
    set /p FINISHED=<"%TMPVAL%"
    del "%TMPVAL%" "%TMPVAL%.err" 2>nul
    rem Unlike the lock collision above this does write a marker. A whole day of
    rem skipped slots would otherwise be invisible: the VPS cannot reach the laptop
    rem while it is offline, so the marker is what tells it, once, what happened.
    set "MARKER=%~dp0data\last_intraday_run.json"
    > "!MARKER!.tmp" (
        echo {
        echo   "kind": "intraday",
        echo   "finished_utc": "!FINISHED!Z",
        echo   "worst_exit": 4,
        echo   "cycle_date": "%DAY%",
        echo   "stages": { "netcheck": 4 },
        echo   "log": "data/log/%DAY%/run_intraday.txt"
        echo }
    )
    move /y "!MARKER!.tmp" "!MARKER!" >nul
    echo [brandmonitor] offline - intraday slot skipped  log: %OUT%
    exit /b 4
)

set "WORST=0"
set "CODE_news=2"
set "CODE_title_gate=2"
set "CODE_title_bodies=2"
set "CODE_body_gate=2"
set "CODE_alert_gate=2"

rem The same lock as run_daily.bat. A 06:00 run still going at 08:00 wins; this
rem slot is skipped without a marker, so the VPS keeps judging the last real run.
2>nul (
    9>"%~dp0data\run.lock" ( call :stages )
) || (
    echo [brandmonitor] another run holds data\run.lock - intraday slot skipped
    >> "%OUT%" echo [intraday] %TIME% skipped: another run holds data\run.lock
    del "%TMPVAL%" "%TMPVAL%.err" 2>nul
    exit /b 3
)

"%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
set /p FINISHED=<"%TMPVAL%"
del "%TMPVAL%" "%TMPVAL%.err" 2>nul
rem Written beside the target and moved over it, so the VPS never reads a partial file.
set "MARKER=%~dp0data\last_intraday_run.json"
> "%MARKER%.tmp" (
    echo {
    echo   "kind": "intraday",
    echo   "finished_utc": "!FINISHED!Z",
    echo   "worst_exit": !WORST!,
    echo   "cycle_date": "%DAY%",
    echo   "stages": { "news": !CODE_news!, "title_gate": !CODE_title_gate!, "title_bodies": !CODE_title_bodies!, "body_gate": !CODE_body_gate!, "alert_gate": !CODE_alert_gate! },
    echo   "log": "data/log/%DAY%/run_intraday.txt"
    echo }
)
move /y "%MARKER%.tmp" "%MARKER%" >nul

echo [brandmonitor] intraday done, worst exit=!WORST!  log: %OUT%
exit /b %WORST%


:stages
call :stage news collect --kind news
call :stage title_gate gate
call :stage title_bodies fetch-bodies --kind news --title-gate-client jt-express
rem News only: regulatory bodies wait for the 06:00 run.
call :stage body_gate body-gate --kind news
call :stage alert_gate alert-gate
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
