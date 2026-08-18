# fetch_html.py
# Robust, site-agnostic fetcher with:
# - HTTP/2 (httpx) first, requests fallback
# - Fast-fail on 406 (no endless backoff)
# - Desktop + Android profiles
# - ISP-proxy escalation for datacenter-IP blocks
# - Explicit decoding for br/gzip/deflate + charset detection

from __future__ import annotations

import io
import re
import gzip
import socket

# pip install httpx requests urllib3 tldextract
import httpx
import requests
import tldextract
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlsplit

# pip install brotli  (or: pip install brotlicffi)
import brotli
import ftfy
import logging
import secrets
import threading

from . import bandwidth
from .crawler_playwright import fetch_html_with_playwright
from .paywall.handler import (
    get_paywall_cfg, fetch_paywall_article, _proxy_cfg, _proxy_url,
    _warm_proxy, _PROXY_WARMUP_URL,
)

logger = logging.getLogger(__name__)
_VERBOSE = False

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 8          # naked datacenter read timeout; a blocked IP should fail fast
PROXY_READ_TIMEOUT = 20   # ISP proxy adds latency, so allow a bit more

# BrightData ISP zone used for the proxy escalation tiers (rotating exit IP).
_PROXY_ZONE = "isp"
# Some ISP exit IPs are themselves Cloudflare-flagged, so rotate through a few
# cheap (~1.5s) HTML fetches before falling back to an expensive render.
PROXY_HTTP_ATTEMPTS = 6

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

# ---------- helpers ----------

def _lang_from_tld(host: str) -> str:
    tld = host.rsplit(".", 1)[-1].lower()
    if tld == "vn":  return "vi-VN,vi;q=0.9,en;q=0.8"
    if tld == "cn":  return "zh-CN,zh;q=0.9,en;q=0.8"
    if tld == "jp":  return "ja-JP,ja;q=0.9,en;q=0.8"
    if tld == "id":  return "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
    if tld in ("kr", "kp"): return "ko-KR,ko;q=0.9,en;q=0.8"
    return "en-US,en;q=0.9"

def _chrome_like_headers(host: str, ua: str, mobile: bool) -> dict:
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        # Keep 'br' — we can decode it; remove 'br' if you want servers to send gzip only.
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": _lang_from_tld(host),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        # Client hints (common on news/CDNs)
        # "sec-ch-ua": "\"Chromium\";v=\"124\", \"Google Chrome\";v=\"124\", \"Not-A.Brand\";v=\"99\"",
        # "sec-ch-ua-mobile": "?1" if mobile else "?0",
        # "sec-ch-ua-platform": "\"Android\"" if mobile else "\"Windows\"",
        # "Sec-Fetch-Site": "none",
        # "Sec-Fetch-Mode": "navigate",
        # "Sec-Fetch-User": "?1",
        # "Sec-Fetch-Dest": "document",
    }

def _with_referer(h: dict, host: str) -> dict:
    hh = dict(h)
    hh["Referer"] = f"https://{host}/"
    return hh

_CHARSET_META_RE = re.compile(br'charset *= *["\']?([A-Za-z0-9_\-]+)', re.I)

def _decode_body(content: bytes, headers: dict, fallback_encoding: str = "utf-8") -> str:
    enc = (headers.get("Content-Encoding") or "").lower()

    # Decode transfer/content-encoding
    if "br" in enc:
        if brotli is None:
            raise RuntimeError("Brotli body received but 'brotli' module not installed. Install 'brotli' or drop 'br' from Accept-Encoding.")
        try:
            content = brotli.decompress(content)
        except Exception:
            # Server lied about brotli encoding, content is not compressed
            pass
    elif "gzip" in enc:
        try:
            content = gzip.decompress(content)
        except gzip.BadGzipFile:
            # Server sent Content-Encoding: gzip but body is not actually gzipped
            pass
    elif "deflate" in enc:
        # Some servers mislabel deflate vs gzip; try both
        try:
            content = gzip.decompress(content)
        except Exception:
            try:
                import zlib
                content = zlib.decompress(content, -zlib.MAX_WBITS)
            except Exception:
                # Server lied about deflate encoding
                pass

    text = None

    # 1) Try charset from HTTP header
    ctype = headers.get("Content-Type", "")
    m = re.search(r"charset *= *([A-Za-z0-9_\-]+)", ctype, re.I)
    if m:
        cs = m.group(1).strip()
        try:
            text = content.decode(cs, errors="replace")
        except Exception:
            pass

    # 2) Try meta charset in the body (first few KB)
    if text is None:
        m2 = _CHARSET_META_RE.search(content[:4096])
        if m2:
            cs = m2.group(1).decode("ascii", "ignore")
            try:
                text = content.decode(cs, errors="replace")
            except Exception:
                pass

    # 3) Guess with charset-normalizer if available
    if text is None:
        try:
            import charset_normalizer as cn
            guess = cn.from_bytes(content).best()
            if guess:
                text = str(guess)
        except Exception:
            pass

    # 4) Fallback
    if text is None:
        text = content.decode(fallback_encoding, errors="replace")

    # 5) Fix any mojibake (e.g., UTF-8 decoded as Latin-1)
    return ftfy.fix_text(text)

# ---------- clients ----------

# HTTP/2 first
_httpx_client = httpx.Client(
    http2=True,
    timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
    follow_redirects=True,
)

# HTTP/1.1 fallback (do NOT retry 406 here)
def _build_requests_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=1,
        read=1,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],  # 406 intentionally excluded
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=40, pool_maxsize=40)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_requests_session = _build_requests_session()

# ---------- attempt funcs ----------

def _has_sufficient_content(html: str) -> bool:
    """Check if HTML has meaningful content (not just a JS shell)."""
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html, "lxml")
        # Count paragraph tags - a real article should have several
        p_tags = soup.find_all("p")
        if len(p_tags) < 3:
            return False
        # Check total text length in paragraphs
        total_text = sum(len(p.get_text(strip=True)) for p in p_tags)
        if total_text < 300:  # Less than 300 chars likely means no article
            return False
        return True
    except Exception:
        # If we can't parse, assume it's OK and let caller handle it
        return True

# ---------- single attempts ----------

def _classify_naked(url: str, headers: dict, use_requests: bool) -> tuple[str, str | None]:
    """One un-proxied (datacenter) attempt.

    Returns one of:
      ("ok",      html)  - HTTP 200 with real article content
      ("shell",   html)  - HTTP 200 but a JS/SPA shell (needs a browser render)
      ("blocked", None)  - non-200, connection reset, or timeout (IP is being refused)
    """
    try:
        if use_requests:
            r = _requests_session.get(
                url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True,
            )
            status, content, hdrs = r.status_code, r.content, r.headers
        else:
            r = _httpx_client.get(url, headers=headers)
            status, content, hdrs = r.status_code, r.content, dict(r.headers)
    except (httpx.HTTPError, requests.RequestException, socket.timeout) as e:
        if _VERBOSE: logger.info("[fetch_html] naked attempt error: %s", e)
        return "blocked", None

    # Record wire bytes so naked (free) fetches show up in the run log's
    # wire_bytes. Cost stays $0 automatically: wire_cost_for() only bills when
    # the proxy tag is ISP, and naked fetches leave it None. We always want the
    # *compressed* on-wire size: httpx exposes it directly; requests doesn't, so
    # we read it from the raw urllib3 stream (raw socket bytes), then fall back
    # to Content-Length, and only as a last resort the decompressed body length.
    wire = getattr(r, "num_bytes_downloaded", 0)
    if not wire:
        try:
            wire = r.raw.tell()  # urllib3: compressed bytes read off the socket
        except Exception:
            wire = 0
        wire = wire or int(hdrs.get("Content-Length") or 0) or len(content)
    bandwidth.add(wire)

    if status != 200:
        if _VERBOSE: logger.info("[fetch_html] naked attempt HTTP %s", status)
        return "blocked", None

    html = _decode_body(content, hdrs)
    return ("ok", html) if _has_sufficient_content(html) else ("shell", html)


def _proxy_httpx(url: str, headers: dict, proxy_cfg: dict) -> tuple[int, str]:
    """One ISP-proxy HTML fetch. Returns (status, decoded_html).

    Records the response wire size on the bandwidth counter (httpx isn't
    CDP-instrumented like Playwright) so the proxy's billed bytes/cost land in
    the run log. Counts every attempt, including failed 403s we still paid for.
    """
    purl = _proxy_url(proxy_cfg)
    with httpx.Client(
        proxy=purl, http2=True,
        timeout=httpx.Timeout(PROXY_READ_TIMEOUT, connect=CONNECT_TIMEOUT * 2),
        follow_redirects=True,
    ) as c:
        r = c.get(url, headers=headers)
        # num_bytes_downloaded is the *compressed* bytes actually read off the
        # wire (what BrightData bills), present even when the response is chunked
        # and has no Content-Length. Fall back to the body length if missing.
        bandwidth.add(getattr(r, "num_bytes_downloaded", 0) or len(r.content))
        return r.status_code, _decode_body(r.content, dict(r.headers))


# Single process-wide pinned ISP session. Like welt/bild's static sessions it
# pins one warm exit IP and reuses it across fetches (so the steady state is one
# attempt), but unlike them it is rotated when that IP gets Cloudflare-flagged.
_isp_session: str | None = None
_isp_lock = threading.Lock()
_warmed_sessions: set[str] = set()  # sessions that have bound an exit IP


def _new_session_token() -> str:
    return "rw" + secrets.token_hex(4)


def _ensure_warm(session: str, cfg: dict) -> bool:
    """Warm a session once so it binds an exit IP, mirroring the paywall flow.

    Without this, a freshly-minted session hits BrightData unbound and the first
    fetch gets `400 Peer not found`; the warmup retries the *same* session until
    a peer is allocated. Cached per session so the pinned IP isn't re-warmed."""
    with _isp_lock:
        if session in _warmed_sessions:
            return True
    if _warm_proxy(cfg, _PROXY_WARMUP_URL):
        with _isp_lock:
            _warmed_sessions.add(session)
        return True
    return False


def _get_isp_session() -> str:
    """The current pinned ISP session, minting one on first use."""
    global _isp_session
    with _isp_lock:
        if _isp_session is None:
            _isp_session = _new_session_token()
        return _isp_session


def _rotate_isp_session(failed: str) -> str:
    """Rotate the pinned session away from a burned exit IP.

    Compare-and-swap: if another worker already rotated past `failed`, adopt the
    current session rather than minting yet another, so a burst of parallel
    fetches hitting the same bad IP converges on one new IP instead of many."""
    global _isp_session
    with _isp_lock:
        if _isp_session == failed or _isp_session is None:
            _isp_session = _new_session_token()
        return _isp_session


def _naked_playwright(url: str) -> str:
    html_bytes = fetch_html_with_playwright(url, timeout_ms=40000)
    return _decode_body(html_bytes, {}) if html_bytes else ""


def _proxy_playwright(url: str) -> str:
    session = _get_isp_session()
    cfg = _proxy_cfg(_PROXY_ZONE, session)
    if not cfg:
        return ""
    html_bytes = fetch_html_with_playwright(
        url, timeout_ms=40000, proxy_cfg=cfg, proxy_name=_PROXY_ZONE,
    )
    return _decode_body(html_bytes, {}) if html_bytes else ""


# ---------- public API ----------

def fetch_html(url: str) -> str:
    """
    Fetch article HTML via a cheap-to-expensive escalation ladder:

      1. naked httpx (datacenter IP) -- HTML only
           200 + content  -> done
           200 + JS shell -> (3) naked Playwright, then (4) ISP-proxy Playwright
           blocked/reset  -> (2) ISP-proxy httpx
      2. ISP-proxy httpx (pinned ISP IP, rotate-on-failure) -- bypasses
           datacenter-IP blocks (Cloudflare etc.); reuses one warm exit IP and
           only rotates to a fresh one when it gets flagged
           200 + content  -> done
           else           -> (4) ISP-proxy Playwright
      3. naked Playwright (full page render) -- for genuine SPA sites
      4. ISP-proxy Playwright (pinned IP, image/css/font/media blocked) -- last resort

    Routing is driven only by HTTP status / connection outcome and the structural
    `_has_sufficient_content` check -- no Cloudflare text heuristics.
    """
    # Paywall sites have their own authenticated Playwright path.
    paywall_cfg = get_paywall_cfg(url)
    if paywall_cfg:
        if _VERBOSE: logger.info("[fetch_html] Paywall site detected, using authenticated Playwright fetch")
        return fetch_paywall_article(url, paywall_cfg)

    ext = tldextract.extract(url)
    host = ".".join([p for p in [ext.domain, ext.suffix] if p])
    desktop = _chrome_like_headers(host, UA_DESKTOP, mobile=False)
    android = _chrome_like_headers(host, UA_ANDROID, mobile=True)

    # --- Tier 1: naked datacenter fetch ---
    naked_attempts = [
        (desktop, False),  # httpx HTTP/2, desktop UA
        (desktop, True),   # requests HTTP/1.1 (covers servers that choke on HTTP/2)
        (android, False),  # httpx HTTP/2, android UA (covers UA gating)
    ]
    for headers, use_requests in naked_attempts:
        kind, html = _classify_naked(url, headers, use_requests)
        if kind == "ok":
            return html
        if kind == "shell":
            # Reachable from the datacenter IP but JS-rendered -> render it.
            if _VERBOSE: logger.info("[fetch_html] 200 but JS shell -> naked Playwright")
            rendered = _naked_playwright(url)
            if rendered and _has_sufficient_content(rendered):
                return rendered
            if _VERBOSE: logger.info("[fetch_html] naked Playwright insufficient -> ISP-proxy Playwright")
            rendered = _proxy_playwright(url)
            if rendered and _has_sufficient_content(rendered):
                return rendered
            raise RuntimeError(f"Failed to fetch {url}: naked Playwright insufficient and ISP-proxy unavailable or insufficient")
        # kind == "blocked" -> try the next naked attempt

    # --- Tier 2: ISP-proxy HTML via the pinned (rotate-on-failure) exit IP ---
    # Reuse the process-wide pinned session so the steady state is one cheap
    # fetch on a warm IP; only when that IP is Cloudflare-flagged do we rotate to
    # a fresh one and retry (each fresh session lands on a new exit IP).
    if _VERBOSE: logger.info("[fetch_html] datacenter blocked -> ISP-proxy httpx")
    proxy_available = False
    session = _get_isp_session()
    for attempt in range(PROXY_HTTP_ATTEMPTS):
        cfg = _proxy_cfg(_PROXY_ZONE, session)
        if not cfg:
            break
        proxy_available = True
        # Bind the session to an exit IP first (handles `400 Peer not found` by
        # retrying the same session); if no peer binds, this IP is unusable.
        if not _ensure_warm(session, cfg):
            if _VERBOSE: logger.info("[fetch_html] proxy attempt %d: warmup found no peer, rotating", attempt + 1)
            session = _rotate_isp_session(session)
            continue
        try:
            status, html = _proxy_httpx(url, desktop, cfg)
        except (httpx.HTTPError, requests.RequestException, socket.timeout) as e:
            if _VERBOSE: logger.info("[fetch_html] proxy httpx attempt %d error: %s", attempt + 1, e)
            session = _rotate_isp_session(session)
            continue
        if status == 200 and _has_sufficient_content(html):
            bandwidth.set_proxy(_PROXY_ZONE, session)  # tag the winning (now pinned) exit IP
            if _VERBOSE: logger.info("[fetch_html] ISP-proxy httpx ok on attempt %d (session %s)", attempt + 1, session)
            return html
        if _VERBOSE: logger.info("[fetch_html] proxy httpx attempt %d -> HTTP %s / insufficient, rotating", attempt + 1, status)
        session = _rotate_isp_session(session)

    if not proxy_available:
        logger.info("[fetch_html] ISP proxy creds missing (BRD_PASS_ISP); falling back to naked Playwright")
        rendered = _naked_playwright(url)
        if rendered and _has_sufficient_content(rendered):
            return rendered
        raise RuntimeError(f"Failed to fetch {url}: datacenter blocked and no proxy creds")

    # --- Tier 4: ISP-proxy Playwright (last resort) ---
    if _VERBOSE: logger.info("[fetch_html] all ISP-proxy httpx attempts failed -> ISP-proxy Playwright")
    rendered = _proxy_playwright(url)
    if rendered and _has_sufficient_content(rendered):
        return rendered

    raise RuntimeError(f"Failed to fetch {url} after naked + ISP-proxy httpx/Playwright")
