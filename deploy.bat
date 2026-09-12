@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "BRANCH=main"
if not defined BRANDMONITOR_DELL_HOST set "BRANDMONITOR_DELL_HOST=100.80.13.120"
if not defined BRANDMONITOR_DELL_USER set "BRANDMONITOR_DELL_USER=dell laptop"
if not defined BRANDMONITOR_VPS_HOST set "BRANDMONITOR_VPS_HOST=100.120.172.43"
if not defined BRANDMONITOR_VPS_USER set "BRANDMONITOR_VPS_USER=root"

where git.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: git.exe is not available.
    exit /b 1
)

where ssh.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: ssh.exe is not available.
    exit /b 1
)

for /f "delims=" %%H in ('git branch --show-current') do set "LOCAL_BRANCH=%%H"
if /i not "!LOCAL_BRANCH!"=="%BRANCH%" (
    echo ERROR: deploy from %BRANCH%, not !LOCAL_BRANCH!.
    exit /b 1
)

echo Checking origin/%BRANCH%...
git fetch origin %BRANCH%
if errorlevel 1 (
    echo ERROR: could not fetch origin/%BRANCH%.
    exit /b 1
)

for /f "delims=" %%H in ('git rev-parse HEAD') do set "LOCAL_HEAD=%%H"
for /f "delims=" %%H in ('git rev-parse origin/%BRANCH%') do set "ORIGIN_HEAD=%%H"
if /i not "!LOCAL_HEAD!"=="!ORIGIN_HEAD!" (
    echo ERROR: local HEAD is not the pushed origin/%BRANCH% commit.
    echo Local:  !LOCAL_HEAD!
    echo Origin: !ORIGIN_HEAD!
    echo Commit and push first, then run deploy.bat again.
    exit /b 1
)

set "DIRTY="
for /f "delims=" %%L in ('git status --porcelain') do set "DIRTY=1"
if defined DIRTY echo NOTE: uncommitted local files are not included; deploying pushed commit !ORIGIN_HEAD!.

echo.
echo Pulling production laptop...
ssh.exe -o BatchMode=yes -o ConnectTimeout=12 -l "%BRANDMONITOR_DELL_USER%" %BRANDMONITOR_DELL_HOST% "cd /d C:\apps\brandmonitor && git pull --ff-only && git log -1 --oneline"
if errorlevel 1 (
    echo ERROR: laptop deployment failed. The VPS was not changed.
    exit /b 1
)

echo.
echo Pulling health-check VPS...
ssh.exe -o BatchMode=yes -o ConnectTimeout=12 %BRANDMONITOR_VPS_USER%@%BRANDMONITOR_VPS_HOST% "cd /var/www/brandmonitor && git pull --ff-only && git log -1 --oneline"
if errorlevel 1 (
    echo ERROR: VPS deployment failed. The laptop was already updated; fix the VPS and rerun.
    exit /b 1
)

echo.
echo Deployment complete: !ORIGIN_HEAD!
exit /b 0
