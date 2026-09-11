"""Central config loader — reads config.json once at import time.

The vendored crawler imports CRAWLER_VERBOSE from here. Keep this module free of
application logic so importing it stays cheap and side-effect free.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.json"
_CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# -- paths --
INPUT_DIR = ROOT / "input"
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "log"

# -- workers --
CRAWLER_WORKERS = _CFG["workers"]["crawler"]

# -- lookback --
NEWS_LOOKBACK_DAYS = _CFG["lookback_days"]["news"]

# -- public body fetches (bounded per invocation) --
BODY_FETCH_LIMIT = _CFG.get("body_fetch", {}).get("limit", 100)
# Seconds enforced between two requests to the SAME host.
BODY_FETCH_DELAY = _CFG.get("body_fetch", {}).get("delay_seconds", 1.0)
BODY_FETCH_TIMEOUT = _CFG.get("body_fetch", {}).get("timeout_seconds", 20)
# Escalate to a headless browser when plain HTTP returns a page with no article
# in it. Off makes those items stay retryable failures rather than costing
# seconds each, which is what a large backfill may want.
BODY_FETCH_BROWSER = _CFG.get("body_fetch", {}).get("browser_escalation", True)
# Regulator PDFs are press releases and reports, not books; a cap keeps one
# 500-page annual report from dominating a batch.
PDF_MAX_PAGES = _CFG.get("body_fetch", {}).get("pdf_max_pages", 20)
# Consecutive retryable failures before a URL is retired as unavailable. Without
# a bound a permanently broken URL costs one request per run for as long as it
# stays configured. `--retry-unavailable` reopens retired URLs.
BODY_FETCH_MAX_ATTEMPTS = _CFG.get("body_fetch", {}).get("max_attempts", 5)

# -- title gate (cheap LLM check before a title-only body fetch) --
_GATE = _CFG.get("title_gate", {})
TITLE_GATE_MODEL = _GATE.get("model", "gpt-5.4-mini-2026-03-17")
TITLE_GATE_REASONING = _GATE.get("reasoning_effort", "low")
TITLE_GATE_BATCH_SIZE = _GATE.get("batch_size", 10)
TITLE_GATE_TIMEOUT = _GATE.get("timeout_seconds", 60)
TITLE_GATE_LOG_KEEP_DAYS = _GATE.get("log_keep_days", 30)

# -- body gate (cheap LLM check between a selected body and the full assessment) --
_BODY_GATE = _CFG.get("body_gate", {})
BODY_GATE_MODEL = _BODY_GATE.get("model", TITLE_GATE_MODEL)
BODY_GATE_REASONING = _BODY_GATE.get("reasoning_effort", "low")
BODY_GATE_TIMEOUT = _BODY_GATE.get("timeout_seconds", 60)
BODY_GATE_WORKERS = _BODY_GATE.get("workers", 4)
BODY_GATE_BODY_CHARS = _BODY_GATE.get("body_chars", 12000)
BODY_GATE_LIMIT = _BODY_GATE.get("limit", 300)

# -- backups --
# A relative dir is resolved against the repo root so a scheduled task's working
# directory cannot silently scatter snapshots somewhere else.
_BACKUP = _CFG.get("backup", {})


def _rooted(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


BACKUP_DIR = _rooted(_BACKUP.get("dir", "data/backups"))
# Off-box target, or None to keep snapshots on this box only. Kept as a string
# here; src/backup.py expands "~" so a config committed from one host does not
# hard-code another host's home directory.
BACKUP_OFFBOX_DIR = _BACKUP.get("offbox_dir") or None
BACKUP_KEEP_DAILY = _BACKUP.get("keep_daily", 14)
BACKUP_KEEP_WEEKLY = _BACKUP.get("keep_weekly", 8)
BACKUP_KEEP_MONTHLY = _BACKUP.get("keep_monthly", 12)
BACKUP_WARN_TOTAL_MB = _BACKUP.get("warn_total_mb", 1024)

# -- verbose --
CRAWLER_VERBOSE = _CFG.get("verbose", {}).get("crawler", False)
