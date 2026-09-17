@echo off
rem brandmonitor daily collection.
rem
rem News, regulatory articles, Safety Gate and parliamentary procedures (Bundestag,
rem Bundesrat, European Parliament), plus title selection, fetch-on-match, and the
rem body gate over every selected body that has no decision yet, followed by one
rem combined internal-review email when the news alert gate finds anything.
rem
rem The weekly assessment and report (run.py export-window / assess / report /
rem verify-report) are deliberately absent from this script. Collection is daily
rem because it is irreversible; the assessment reads only the database, so it is
rem weekly and run by hand while the schema settles - see todo.md 1.1.
rem
rem Stages never chain on success: collection is source-specific and the two source
rem lists are independent, so a news failure must not cancel regulatory collection.
rem Each stage records its own code and the run reports the worst one.
rem
rem   0  every stage completed
rem   1  a stage produced nothing usable (every source in it failed)
rem   2  a stage aborted, or this script could not start one
rem   3  another run holds the lock; nothing was attempted
rem   4  this host has no internet; nothing was attempted

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

echo.>> "%OUT%"
echo ======== brandmonitor daily %DATE% %TIME% ========>> "%OUT%"

rem Monitoring only: every exit below records this pass with `run.py record-pass`
rem (src/monitoring.py), whose exit code is ignored - a pass that could not be
rem recorded is a missing monitoring row, never a different outcome.
"%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
set /p STARTED=<"%TMPVAL%"

rem Preflight. A run that starts during an outage stores a partial day, spends
rem every stage's timeouts, and leaves a marker whose stage codes say where each
rem stage's first request happened to fail rather than naming the one cause.
rem Nothing is lost by skipping: every collector resumes from its own watermark
rem and every queue is durable. There is deliberately no wait-and-retry loop -
rem see src/net.py. netcheck exits 4 for offline and 2 only if it broke itself,
rem in which case the run proceeds as it did before this check existed.
echo.>> "%OUT%"
echo -------- netcheck -------->> "%OUT%"
"%PY%" run.py netcheck >> "%OUT%" 2>&1
if errorlevel 4 (
    "%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
    set /p FINISHED=<"%TMPVAL%"
    del "%TMPVAL%" "%TMPVAL%.err" 2>nul
    rem One pseudo-stage, so the marker keeps its "worst_exit equals the highest
    rem stage code" invariant and health/check.py reads 4 back as `offline`.
    > "%~dp0data\last_run.json" (
        echo {
        echo   "finished_utc": "!FINISHED!Z",
        echo   "worst_exit": 4,
        echo   "cycle_date": "%DAY%",
        echo   "stages": { "netcheck": 4 },
        echo   "log": "data/log/%DAY%/run_daily.txt"
        echo }
    )
    "%PY%" run.py record-pass --kind daily --outcome offline --started "!STARTED!" --marker "%~dp0data\last_run.json" >> "%OUT%" 2>&1
    echo [brandmonitor] offline - no stage attempted  log: %OUT%
    exit /b 4
)

set "WORST=0"
set "CODE_netcheck=0"
set "CODE_news=2"
set "CODE_title_gate=2"
set "CODE_title_bodies=2"
set "CODE_regulatory=2"
set "CODE_safety_gate=2"
set "CODE_dip=2"
set "CODE_ep_procedures=2"
set "CODE_body_gate=2"
set "CODE_dip_docs=2"
set "CODE_alert_gate=2"
set "CODE_backup=2"
set "CODE_canary=2"
set "CODE_quality_health=2"

rem One SQLite writer at a time. A daily run must not collide with a manual
rem backfill; the handle on the lock file is held for as long as the block runs.
2>nul (
    9>"%~dp0data\run.lock" ( call :stages )
) || (
    echo [brandmonitor] another run holds data\run.lock - nothing attempted
    "%PY%" run.py record-pass --kind daily --outcome lock_skipped --started "!STARTED!" >> "%OUT%" 2>&1
    exit /b 3
)

rem The network was up when this run started, so a stage failing is normally the
rem source's problem. Ask once more when anything failed: DNS died five seconds
rem after news collection finished on 2026-09-16, and the marker then described
rem where each later stage's first request happened to fail rather than naming
rem the one cause. Cheap, and it runs after the lock is released.
if !WORST! GTR 0 (
    echo.>> "%OUT%"
    echo -------- netcheck ^(after a non-zero run^) -------->> "%OUT%"
    "%PY%" run.py netcheck >> "%OUT%" 2>&1
    if errorlevel 4 (
        set "CODE_netcheck=4"
        set "WORST=4"
        echo [brandmonitor] the network is down now; the stage failures above are one cause
    )
)

"%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat()[:19])" > "%TMPVAL%" 2>nul
set /p FINISHED=<"%TMPVAL%"
del "%TMPVAL%" "%TMPVAL%.err" 2>nul
> "%~dp0data\last_run.json" (
    echo {
    echo   "finished_utc": "!FINISHED!Z",
    echo   "worst_exit": !WORST!,
    echo   "cycle_date": "%DAY%",
    echo   "stages": { "netcheck": !CODE_netcheck!, "news": !CODE_news!, "title_gate": !CODE_title_gate!, "title_bodies": !CODE_title_bodies!, "regulatory": !CODE_regulatory!, "safety_gate": !CODE_safety_gate!, "dip": !CODE_dip!, "ep_procedures": !CODE_ep_procedures!, "body_gate": !CODE_body_gate!, "dip_docs": !CODE_dip_docs!, "alert_gate": !CODE_alert_gate!, "backup": !CODE_backup!, "canary": !CODE_canary!, "quality_health": !CODE_quality_health! },
    echo   "log": "data/log/%DAY%/run_daily.txt"
    echo }
)
"%PY%" run.py record-pass --kind daily --outcome completed --started "!STARTED!" --marker "%~dp0data\last_run.json" >> "%OUT%" 2>&1

echo [brandmonitor] done, worst exit=!WORST!  log: %OUT%
exit /b %WORST%


:stages
rem Always returns 0 so the lock block above can tell "did not start" from "ran
rem badly"; the outcome travels in WORST.
call :stage news collect --kind news
rem Not chained on news succeeding: a crawl that aborted half way still stored
rem items worth gating, and when no crawl ran today the gate finds its run too old
rem and does nothing rather than repeating yesterday.
call :stage title_gate gate
rem Independent from the gate's exit code: it also retries URLs queued by an
rem earlier keep. New keeps come from the client's retained JSONL decision log;
rem title-only sources are never bulk-fetched.
call :stage title_bodies fetch-bodies --kind news --title-gate-client jt-express
rem News alerts are an extra pass over the news items admitted above. It runs
rem right after news collection, gating and body fetch, ahead of the regulatory
rem stages, so a same-day news alert reaches the digest without waiting on
rem regulatory/Safety Gate/DIP/EP collection or the body gate. It reads only
rem news bodies, so it never depended on those stages anyway.
call :stage alert_gate alert-gate
call :stage regulatory collect --kind regulatory
call :stage safety_gate collect-safety-gate
rem Parliamentary procedures are stored as regulatory items with a body composed
rem from the record, so they reach the body gate below without a fetch.
call :stage dip collect-dip
call :stage ep_procedures collect-ep
rem After the collections, so the day's full-text news and regulatory bodies are
rem stored. Title-gate keeps skip this second cheap gate and later go directly to
rem the full assessor. It reads every other selected body without a decision, not
rem only today's; config.json's limit caps each tier per run.
call :stage body_gate body-gate
rem The documents behind the DIP procedures the gate judged relevant - the answer,
rem the bill. DIP publishes a Drucksache's text days after its date, so a document
rem without text waits in the queue for a later run rather than failing this one.
call :stage dip_docs fetch-dip-docs --client jt-express
rem Backup is the last stage that handles the corpus, so the snapshot carries the
rem day's collection rather than yesterday's. The observers after it read the
rem final database without modifying it and publish their own atomic JSON files.
call :stage backup backup
rem The publisher canary is switched off in health\canaries.json ("enabled": false)
rem while the collection config is still moving: a URL we lack is more likely to be
rem our own churn than a publisher change, and it is not on the polite path that
rem src\polite_http.py put collection on. health\canary.py carries the reasons and
rem what it must do before it comes back. The stage stays here and exits 0 without
rem sending a request, so the switch lives in one file rather than two.
call :observer canary health\canary.py --cycle-date "%DAY%"
call :observer quality_health health\analyze.py --cycle-date "%DAY%"
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


:observer
rem %1 stage name, %2 script path, %3.. arguments. A finding is written inside
rem the quality JSON and exits zero; nonzero means the observer itself broke.
echo.>> "%OUT%"
echo -------- %1 -------->> "%OUT%"
"%PY%" "%~2" %3 %4 %5 %6 %7 %8 %9 >> "%OUT%" 2>&1
set "CODE=!ERRORLEVEL!"
set "CODE_%1=!CODE!"
echo [%1] exit=!CODE!>> "%OUT%"
echo [brandmonitor] %1 exit=!CODE!
if !CODE! GTR !WORST! set "WORST=!CODE!"
exit /b 0
