#!/usr/bin/env python3
"""Inspect the production laptop from any checkout without hand-built SSH lines.

The database, logs and run markers live only on the laptop (CLAUDE.md, "Hosts and
secrets"); a development machine has none of them. Reaching them by hand keeps
hitting the same three traps: the laptop's SSH shell is Windows PowerShell with a
German locale, so ``python -c "..."`` and ``set X=... &`` both fail to parse; a
relative ``data/...`` path resolves against the SSH user's home rather than the
repository; and Chinese output needs UTF-8 on both ends.

Everything here goes over stdin instead of the command line, so nothing is ever
quoted twice. When this file runs on the laptop itself (the database is present
beside it) the same commands run locally.

    python tools/laptop.py sql "SELECT id, kind, started_at, note FROM run ORDER BY id DESC LIMIT 5"
    python tools/laptop.py sql --json "SELECT payload FROM alert_decision ORDER BY id DESC LIMIT 1"
    python tools/laptop.py py scratch/query.py      # script runs in the laptop's repo, venv, cwd
    python tools/laptop.py ps "Get-ScheduledTaskInfo -TaskName brandmonitor-daily"
    python tools/laptop.py status                   # deployed commit, tasks, run markers
    python tools/laptop.py admin                    # the read-only monitor, tunnelled here

``sql`` opens the database read-only (``mode=ro`` and ``query_only``), so it is
safe while the pipeline runs. ``py`` and ``ps`` are not restricted: they are for
inspection, and anything that writes still needs the owner's confirmation.

``admin`` starts ``tools/admin.py`` on the laptop, bound to its loopback address,
and forwards the port over the same SSH session: nothing listens on the network,
no firewall rule is needed, and the server exits when the session ends. When the
laptop's always-on ``brandmonitor-admin`` task is already serving, the session only
forwards to it. From a tailnet device, https://desktop-paf96vp.tail33e56b.ts.net/
needs neither.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

HOST = "100.80.13.120"
USER = "dell laptop"
REMOTE_ROOT = r"C:\apps\brandmonitor"
REMOTE_PYTHON = REMOTE_ROOT + r"\.venv\Scripts\python.exe"

LOCAL_ROOT = Path(__file__).resolve().parent.parent
ON_LAPTOP = (LOCAL_ROOT / "data" / "brandmonitor.sqlite3").exists()

SQL_TEMPLATE = """
import json, sqlite3
sql, as_json, width = {sql!r}, {as_json!r}, {width!r}
conn = sqlite3.connect("file:data/brandmonitor.sqlite3?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
cur = conn.execute(sql)
names = [d[0] for d in cur.description or ()]
def cell(value):
    text = "" if value is None else str(value)
    text = text.replace("\\t", " ").replace("\\r", " ").replace("\\n", " | ")
    return text if not width or len(text) <= width else text[:width - 1] + "…"
if not as_json:
    print("\\t".join(names))
count = 0
for row in cur:
    count += 1
    if as_json:
        print(json.dumps(dict(zip(names, row)), ensure_ascii=False))
    else:
        print("\\t".join(cell(v) for v in row))
if not as_json:
    print(f"({{count}} row{{'s' if count != 1 else ''}})")
"""

STATUS_PS = r"""
Set-Location C:\apps\brandmonitor
'--- deployed commit'
git log -1 --format='%h %ad %s' --date=iso
'--- uncommitted'
git status --short
'--- scheduled tasks'
foreach ($name in 'brandmonitor-daily', 'brandmonitor-intraday') {
  try {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction Stop
    $i = $t | Get-ScheduledTaskInfo
    # A disabled task still reports its trigger's NextRunTime; it will not run then.
    $next = if ($t.State -eq 'Disabled') { 'none (disabled)' } else { $i.NextRunTime }
    '{0} [{1}]: last {2} result {3}, next {4}' -f $name, $t.State, $i.LastRunTime, $i.LastTaskResult, $next
  } catch { "${name}: not registered" }
}
foreach ($marker in 'data\last_run.json', 'data\last_intraday_run.json') {
  "--- $marker"
  if (Test-Path $marker) { Get-Content -Raw -Encoding UTF8 $marker } else { 'absent' }
}
'--- run.lock'
# .NET resolves relative paths against the process directory, not Set-Location.
$lock = 'C:\apps\brandmonitor\data\run.lock'
if (Test-Path $lock) {
  try { [IO.File]::Open($lock, 'Open', 'ReadWrite', 'None').Close(); 'free' }
  catch [System.IO.IOException] { 'held (a run is in progress)' }
} else { 'absent' }
"""


def _python_prelude() -> str:
    root = str(LOCAL_ROOT) if ON_LAPTOP else REMOTE_ROOT
    return (f"import os, sys\nos.chdir({root!r})\nsys.path.insert(0, {root!r})\n"
            "sys.argv = ['laptop-script']\n")


def run_python(source: str) -> int:
    """Run Python source in the laptop repository with its virtualenv."""
    script = (_python_prelude() + source).encode("utf-8")
    if ON_LAPTOP:
        command = [sys.executable, "-X", "utf8", "-"]
    else:
        # The remote shell is PowerShell; this line has no quotes or spaces to parse.
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                   "-l", USER, HOST, f"{REMOTE_PYTHON} -X utf8 -"]
    return subprocess.run(command, input=script).returncode


def run_powershell(source: str) -> int:
    """Run a PowerShell script on the laptop, passed base64-encoded."""
    # Progress records otherwise arrive over SSH as a CLIXML blob on stderr.
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
              "$ProgressPreference = 'SilentlyContinue'\n" + source)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    ps = f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"
    if ON_LAPTOP:
        return subprocess.run(ps.split(" ")).returncode
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           "-l", USER, HOST, ps]).returncode


def run_admin(port: int, open_browser: bool) -> int:
    """Serve the read-only monitor from the laptop's data and show it here."""
    url = f"http://127.0.0.1:{port}/"
    if ON_LAPTOP:
        # --exit-on-stdin-eof: when the always-on server already holds the port,
        # admin.py waits on our pipe instead of returning at once.
        command = [sys.executable, str(LOCAL_ROOT / "tools" / "admin.py"), "--port", str(port),
                   "--exit-on-stdin-eof"]
    else:
        # The server binds the laptop's loopback; -L carries it over this session, and
        # --exit-on-stdin-eof ends it when the session does.
        remote = (f"{REMOTE_PYTHON} {REMOTE_ROOT}\\tools\\admin.py --port {port} "
                  f"--exit-on-stdin-eof")
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                   "-o", "ExitOnForwardFailure=yes", "-L", f"{port}:127.0.0.1:{port}",
                   "-l", USER, HOST, remote]
    print(f"monitor: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        timer = threading.Timer(4.0, webbrowser.open, (url,))
        timer.daemon = True
        timer.start()
    # A pipe of our own, never written: the remote stdin then stays open exactly as
    # long as this ssh process lives, whatever this script's own stdin is.
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    sql = commands.add_parser("sql", help="read-only query against the production database")
    sql.add_argument("query", help="SQL text, or - to read it from stdin")
    sql.add_argument("--json", action="store_true", help="one JSON object per row, untruncated")
    sql.add_argument("--width", type=int, default=200,
                     help="truncate table cells to this many characters (0 = never)")

    py = commands.add_parser("py", help="run a local Python file (or - for stdin) on the laptop")
    py.add_argument("script")

    ps = commands.add_parser("ps", help="run a PowerShell command on the laptop")
    ps.add_argument("script", help="PowerShell text, or - to read it from stdin")

    commands.add_parser("status", help="deployed commit, scheduled tasks, run markers, lock")

    admin = commands.add_parser("admin", help="open the read-only monitor (tools/admin.py) "
                                              "from the laptop's data")
    admin.add_argument("--port", type=int, default=8765)
    admin.add_argument("--no-browser", action="store_true", help="only print the URL")

    args = parser.parse_args(argv)
    if args.command == "admin":
        return run_admin(args.port, not args.no_browser)
    if args.command == "sql":
        query = sys.stdin.read() if args.query == "-" else args.query
        return run_python(SQL_TEMPLATE.format(sql=query, as_json=args.json,
                                              width=args.width))
    if args.command == "py":
        source = (sys.stdin.read() if args.script == "-"
                  # utf-8-sig: Windows PowerShell 5.1 writes a BOM Python rejects.
                  else Path(args.script).read_text(encoding="utf-8-sig"))
        return run_python(source)
    if args.command == "ps":
        return run_powershell(sys.stdin.read() if args.script == "-" else args.script)
    return run_powershell(STATUS_PS)


if __name__ == "__main__":
    sys.exit(main())
