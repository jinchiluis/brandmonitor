# fetch_html.py
# Robust, site-agnostic fetcher with:
# - HTTP/2 (httpx) first, requests fallback
# - Fast-fail on 406 (no endless backoff)
# - Referer + Android/desktop profiles
# - AMP fallbacks
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
from .crawler_playwright import fetch_html_with_playwright
from .crawler_brightdata import fetch_html_with_api as fetch_html_with_brightdata_api
from .paywall.handler import get_paywall_cfg, fetch_paywall_article
from src.crawler_news.source_loader import sources
from src.logger import get_logger

logger = get_logger(__name__)
from src.config import CRAWLER_VERBOSE as _VERBOSE

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 12

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

def _amp_variants(url: str) -> list[str]:
    out = []
    if "outputType=amp" not in url:
        sep = "&" if "?" in url else "?"
        out.append(url + f"{sep}outputType=amp")
    if not re.search(r"/amp(/|$)", url):
        out.append(url.rstrip("/") + "/amp")
    return out

def should_try_amp(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    if host.endswith("kompas.com"):
        return False  # Kompas AMP is heavily gated
    return True

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

def _attempt_httpx(url: str, headers: dict) -> str:
    r = _httpx_client.get(url, headers=headers)
    if r.status_code == 406:
        # fast-fail so we can switch profile / AMP quickly
        raise httpx.HTTPStatusError("406 Not Acceptable", request=r.request, response=r)
    r.raise_for_status()
    html = _decode_body(r.content, dict(r.headers))
    # Check if we got a JS-only shell with no content
    if not _has_sufficient_content(html):
        raise httpx.HTTPStatusError("Insufficient content (JS-only page)", request=r.request, response=r)
    return html

def _attempt_requests(url: str, headers: dict) -> str:
    r = _requests_session.get(
        url, headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )
    if r.status_code == 406:
        raise requests.HTTPError("406 Not Acceptable", response=r)
    r.raise_for_status()
    html = _decode_body(r.content, r.headers)
    # Check if we got a JS-only shell with no content
    if not _has_sufficient_content(html):
        raise requests.HTTPError("Insufficient content (JS-only page)", response=r)
    return html

# ---------- public API ----------

def fetch_html(url: str) -> str:
    """
    Fetch HTML robustly (HTTP/2 first), with fast-fail on 406 and AMP fallbacks.
    If config indicates brightdata=True for this URL, skips straight to BrightData API.
    Returns decoded unicode HTML string.
    Raises on final failure.
    """
    # Check for paywall sites (login-required)
    paywall_cfg = get_paywall_cfg(url)
    if paywall_cfg:
        if _VERBOSE: logger.info("[fetch_html] Paywall site detected, using authenticated Playwright fetch")
        return fetch_paywall_article(url, paywall_cfg)

    # Check if this site requires BrightData upfront
    if sources.is_brightdata_enabled(url):
        logger.info("[fetch_html] Site configured for brightdata=True, using BrightData API directly")
        html_bytes = fetch_html_with_brightdata_api(url, timeout=120)
        if html_bytes and len(html_bytes) > 1000:
            txt = _decode_body(html_bytes, {})
            if len(txt) > 0:
                logger.info("[fetch_html] BrightData API success, returning %d chars", len(txt))
                return txt
            else:
                logger.info("[fetch_html] BrightData API returned empty content")
        else:
            logger.info("[fetch_html] BrightData API failed or insufficient content")
        # If BrightData fails, raise error instead of falling back
        # (if site needs BrightData, regular methods won't work anyway)
        raise RuntimeError(f"BrightData API required for {url} but failed")

    ext = tldextract.extract(url)
    host = ".".join([p for p in [ext.domain, ext.suffix] if p])

    profiles = [
        _chrome_like_headers(host, UA_DESKTOP, mobile=False),
        _chrome_like_headers(host, UA_ANDROID, mobile=True),
    ]

    def _tries(u: str):
        # HTTP/2 attempts
        yield ("h2-desktop", u, profiles[0])
        yield ("h2-desktop+ref", u, _with_referer(profiles[0], host))
        yield ("h2-android", u, profiles[1])
        yield ("h2-android+ref", u, _with_referer(profiles[1], host))
        # HTTP/1.1 attempts
        yield ("req-desktop", u, profiles[0])
        yield ("req-desktop+ref", u, _with_referer(profiles[0], host))
        yield ("req-android", u, profiles[1])
        yield ("req-android+ref", u, _with_referer(profiles[1], host))

    last_err = None

    # Primary URL
    for label, u, h in _tries(url):
        try:
            if label.startswith("h2"):
                return _attempt_httpx(u, h)
            else:
                return _attempt_requests(u, h)
        except (httpx.HTTPStatusError, requests.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # For unexpected 4xx not in the soft list, bail early
            if status not in (403, 406, 429, 500, 502, 503, 504, None):
                last_err = e
                break
            last_err = e
            continue
        except (httpx.TimeoutException, httpx.NetworkError,
                requests.ConnectionError, requests.Timeout, socket.timeout) as e:
            last_err = e
            continue

    # AMP variants
    if should_try_amp(url):
        for amp in _amp_variants(url):
            for label, u, h in _tries(amp):
                try:
                    if label.startswith("h2"):
                        return _attempt_httpx(u, h)
                    else:
                        return _attempt_requests(u, h)
                except Exception as e:
                    last_err = e
                    continue

    ##### Last try: PLAYWRIGHT! (with caching for sites already crawled)
    logger.info("[fetch_html] Falling back to Playwright for %s", url)
    try:
        html_bytes = fetch_html_with_playwright(url, timeout_ms=40000)
        if html_bytes:
            logger.info("[fetch_html] Playwright returned %d bytes", len(html_bytes))
            txt = _decode_body(html_bytes, {})
            # For Playwright results, trust any non-empty content
            # Playwright handles bot detection, so if we got HTML, it's likely valid
            # Don't check for keywords - news articles can mention "forbidden", "captcha", etc.
            if len(txt) > 0:
                logger.info("[fetch_html] Playwright success, returning %d chars", len(txt))
                return txt
            else:
                logger.info("[fetch_html] Playwright returned empty page")
        else:
            logger.info("[fetch_html] Playwright returned empty bytes")
    except Exception as e:
        logger.info("[fetch_html] Playwright exception: %s", e)
        last_err = e

    # Only raise errors if we truly failed everything (including Playwright)
    if isinstance(last_err, (httpx.HTTPError, requests.HTTPError)):
        raise last_err
    raise RuntimeError(f"Failed to fetch after fallbacks: {last_err}")
