# proxy.py
# Bright Data ISP-proxy fallback for the cheap-HTML fetch ladder.
#
# Reached when a naked (datacenter-IP) fetch is blocked (403 / reset / timeout)
# by a source that was never explicitly flagged for Bright Data. Deliberately
# narrow in scope: plain HTTP only, one rotate-on-failure tier, no Playwright
# render and no paywall login. See backup_plan.md Section 4 for why a proxied
# browser render and paywall proxy support for bild.de/welt.de are out of
# scope here.
#
# Ported from the ladder proven on this same VPS under rewriter-scrape.service
# (documented in /var/www/rewriter/vendor/html_scrape_with_proxy.md), trimmed
# to the naked-httpx -> ISP-proxy-httpx tiers.

from __future__ import annotations

import os
import secrets
import threading
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from src.logger import get_logger

logger = get_logger(__name__)

_BRD_HOST = "brd.superproxy.io"
_BRD_PORT = 33335
_BRD_CUSTOMER = "brd-customer-hl_8bdecc95"
_BRD_ISP_ZONE = "isp_proxy1"
_PROXY_WARMUP_URL = "https://www.gstatic.com/generate_204"

CONNECT_TIMEOUT = 6
READ_TIMEOUT = 20       # proxy adds latency over the naked datacenter fetch
HTTP_ATTEMPTS = 6       # exit IPs to rotate through before giving up


def proxy_cfg(session: str | None = None) -> dict | None:
    """Bright Data ISP proxy credentials as a requests-style proxy dict, or
    None if BRD_PASS_ISP isn't set (proxy fallback silently unavailable)."""
    password = os.environ.get("BRD_PASS_ISP", "")
    if not password:
        return None
    username = f"{_BRD_CUSTOMER}-zone-{_BRD_ISP_ZONE}"
    if session:
        safe_session = "".join(ch for ch in session if ch.isalnum() or ch in "_-")
        if safe_session:
            username = f"{username}-session-{safe_session}"
    url = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{_BRD_HOST}:{_BRD_PORT}"
    return {"http": url, "https": url}


def has_sufficient_content(html: str) -> bool:
    """Structural check that this is a real article page, not a Cloudflare
    interstitial or JS shell: >=3 <p> tags and >=300 chars of paragraph text
    in the raw HTML. Never text-match for challenge-page phrases — those can
    legitimately appear in real article prose."""
    try:
        soup = BeautifulSoup(html, "lxml")
        p_tags = soup.find_all("p")
        if len(p_tags) < 3:
            return False
        return sum(len(p.get_text(strip=True)) for p in p_tags) >= 300
    except Exception:
        return True


# Single process-wide pinned ISP session: reused across fetches so the steady
# state is one warm-IP attempt, and rotated only when that IP gets flagged.
_isp_session: str | None = None
_isp_lock = threading.Lock()
_warmed_sessions: set[str] = set()


def _new_session_token() -> str:
    return "bm" + secrets.token_hex(4)


def _get_isp_session() -> str:
    global _isp_session
    with _isp_lock:
        if _isp_session is None:
            _isp_session = _new_session_token()
        return _isp_session


def _rotate_isp_session(failed: str) -> str:
    global _isp_session
    with _isp_lock:
        if _isp_session == failed or _isp_session is None:
            _isp_session = _new_session_token()
        return _isp_session


def _warm(cfg: dict, attempts: int = 3) -> bool:
    """Bind a fresh session to an exit IP before the first real fetch.
    Bright Data returns 400 Peer not found until a peer is allocated for a new
    session, so retry the *same* session rather than rotating."""
    for _ in range(attempts):
        try:
            with requests.get(_PROXY_WARMUP_URL, proxies=cfg, timeout=(3, 8), stream=True):
                return True
        except requests.RequestException:
            continue
    return False


def _ensure_warm(session: str, cfg: dict) -> bool:
    with _isp_lock:
        if session in _warmed_sessions:
            return True
    if _warm(cfg):
        with _isp_lock:
            _warmed_sessions.add(session)
        return True
    return False


def try_fetch(url: str, headers: dict) -> requests.Response | None:
    """Cheap-HTML-only ISP-proxy fallback: rotate through pinned exit IPs,
    gated by has_sufficient_content, until one returns a real page.

    Returns the winning `requests.Response`, or None if BRD_PASS_ISP is unset
    or every attempt was blocked/insufficient. Never raises for ordinary fetch
    failures — callers should fall through to whatever they did before this
    fallback existed (typically Playwright).
    """
    session = _get_isp_session()
    for attempt in range(HTTP_ATTEMPTS):
        cfg = proxy_cfg(session)
        if not cfg:
            if attempt == 0:
                logger.info("[proxy] BRD_PASS_ISP not set, skipping ISP-proxy fallback")
            return None
        if not _ensure_warm(session, cfg):
            logger.info("[proxy] attempt %d: warmup found no peer, rotating", attempt + 1)
            session = _rotate_isp_session(session)
            continue
        try:
            resp = requests.get(
                url, headers=headers, proxies=cfg,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException as e:
            logger.info("[proxy] attempt %d error: %s", attempt + 1, e)
            session = _rotate_isp_session(session)
            continue
        if resp.status_code == 200 and has_sufficient_content(resp.text):
            logger.info(
                "[proxy] ISP-proxy fetch ok on attempt %d (session %s) for %s",
                attempt + 1, session, url,
            )
            return resp
        logger.info(
            "[proxy] attempt %d -> HTTP %s / insufficient content, rotating",
            attempt + 1, resp.status_code,
        )
        session = _rotate_isp_session(session)
    return None
