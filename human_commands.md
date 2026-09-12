.\deploy.bat

ssh -l "dell laptop" 100.80.13.120 "cd /d C:\apps\brandmonitor && run_daily.bat"

ssh -l "dell laptop" 100.80.13.120
powershell -NoProfile
Get-Content C:\apps\brandmonitor\data\log\2026-09-12\run_daily.txt -Wait -Tail 20
