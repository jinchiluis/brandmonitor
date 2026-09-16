.\deploy.bat

python tools/laptop.py admin          # read-only monitor, opens http://127.0.0.1:8765/ ; Ctrl+C ends it

ssh -l "dell laptop" 100.80.13.120 "cd /d C:\apps\brandmonitor && run_daily.bat"

ssh -l "dell laptop" 100.80.13.120
powershell -NoProfile
Get-Content C:\apps\brandmonitor\data\log\2026-09-12\run_daily.txt -Wait -Tail 20

human todos:
- zeit with proxy probe
- admin tool for logs
- new run
- paywall?