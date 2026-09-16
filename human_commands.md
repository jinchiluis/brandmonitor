# Deploy
.\deploy.bat

# Admin Monitor
https://desktop-paf96vp.tail33e56b.ts.net/#overview    

# Laptop Server Status
python tools/laptop.py status

# Manual commands
ssh -l "dell laptop" 100.80.13.120 "cd /d C:\apps\brandmonitor && run_daily.bat"

ssh -l "dell laptop" 100.80.13.120
powershell -NoProfile
Get-Content C:\apps\brandmonitor\data\log\2026-09-12\run_daily.txt -Wait -Tail 20

# Human todos:
- zeit with proxy probe
- admin tool for logs
- new run
- paywall?