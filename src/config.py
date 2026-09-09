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

# -- verbose --
CRAWLER_VERBOSE = _CFG.get("verbose", {}).get("crawler", False)
