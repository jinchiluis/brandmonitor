#!/usr/bin/env python3
"""
News Crawler MVP
-----------------
Crawls a news site using robots.txt-declared sitemaps and (optionally) a few common RSS/Atom feed URLs.
Outputs flat JSONL with: site, title, url, published_at, author, section, paywalled, summary, source, crawled_at.

Design principles:
- No per-site adapters
- Europe/Berlin timezone for date filtering
- Fetches missing titles from article HTML when not provided by RSS/sitemap


"""
from __future__ import annotations
import dataclasses
import os
import re
import gzip, io
import time, random
from datetime import datetime, timedelta, timezone

from src.config import CRAWLER_VERBOSE as _VERBOSE
from typing import Iterable, List, Optional, Tuple, Dict, Set
from urllib.parse import urljoin, urlparse, urlunparse, urlsplit, urlunsplit, parse_qsl, urlencode
import tldextract
import xml.etree.ElementTree as ET
import requests
from lxml import etree
import feedparser
from dateutil import parser as dateparser
from .crawler_html_utils import enrich_dates_light, fetch_html, fetch_title_fallback
from .crawler_playwright import fetch_html_with_playwright
from src.crawler_news.source_loader import sources
from src.logger import get_logger
logger = get_logger(__name__)


# Use zoneinfo if available (Python 3.9+), else fallback to fixed offset CET/CEST naive handling
# Robust timezone handling for Windows/conda (missing IANA tzdb)
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

BERLIN_TZ = None
if ZoneInfo is not None:
    try:
        BERLIN_TZ = ZoneInfo("Europe/Berlin")
    except Exception:
        BERLIN_TZ = None
if BERLIN_TZ is None:
    try:
        from dateutil.tz import gettz  # fallback using dateutil tzdata
        tz = gettz("Europe/Berlin")
        if tz is not None:
            BERLIN_TZ = tz
    except Exception:
        BERLIN_TZ = None
# Last resort: UTC (we'll still label outputs with ISO including +00:00)
if BERLIN_TZ is None:
    BERLIN_TZ = timezone.utc

USER_AGENT = "NewsMVPBot/0.1 (+contact@example.com)"
REQ_TIMEOUT = 20
SLEEP_BASE_SEC = 0.25  # polite pause between requests

COMMON_FEED_PATHS = [
    "/feed", "/feeds", "/rss", "/rss.xml", "/atom.xml",
    "/index.rss", "/news/rss",
]


HTML_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        # extra “sec-*” hints that some WAFs check
        # "sec-fetch-site": "same-origin",
        # "sec-fetch-mode": "navigate",
        # "sec-fetch-dest": "document",
        # "upgrade-insecure-requests": "1",
    }

SITEMAP_HEADERS = {
    "User-Agent": HTML_HEADERS["User-Agent"],  # <- browser UA otherwise some reject sitemap fetches
    "Accept": "application/xml,text/xml,application/rss+xml,*/*;q=0.1",
    "Accept-Encoding": "gzip, deflate",
}

@dataclasses.dataclass
class ArticleHint:
    url: str
    published_at: Optional[datetime]
    title: Optional[str]
    source: str  # "sitemap" or "rss"

@dataclasses.dataclass
class ArticleRecord:
    site: str
    title: Optional[str]
    url: str
    published_at: Optional[str]  # ISO string
    crawled_at: str  # ISO string


# ------------------ Utilities ------------------

def feature_allowed(url: str, feature: str) -> bool:
    """
    Check if a feature is allowed for a given URL.
    Args:
        url: The URL to check (will extract host)
        feature: The feature to check (e.g., 'sitemap', 'feeds', 'gov', 'frontpage', 'brightdata')
    Returns:
        True if feature is allowed, False otherwise
    """
    rules = sources.get_site_rules(url)
    if rules is None:
        return True  # URL not in our sources → allow everything
    return rules.get(feature, False)  # default to False if not specified

def now_berlin() -> datetime:
    return datetime.now(tz=BERLIN_TZ)

def now_berlin_iso() -> str:
    return now_berlin().isoformat()

def normalize_url(url: str) -> str:
    # remove utm parameters, fragments, normalize scheme/host case
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    query = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"mc_cid", "mc_eid"}]
    new_query = urlencode(query, doseq=True)
    parts = parts._replace(query=new_query, fragment="")
    return urlunsplit(parts)


def domain_of(url: str) -> str:
    return urlparse(url).netloc


def parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = dateparser.parse(val)
        if dt is None:
            return None
        # normalize to Europe/Berlin tz-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BERLIN_TZ)
    except Exception:
        return None


def in_range(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    if not dt:
        return False
    return start <= dt <= end


def url_matches_dirs(url: str, allowed_dirs: Optional[List[str]]) -> bool:
    """
    Check if a URL path matches any of the allowed_dirs.

    Args:
        url: The URL to check
        allowed_dirs: List of allowed directory paths (e.g., ["news", "world"])
                     If None or empty, all URLs are allowed

    Returns:
        True if URL matches allowed directories or no restriction exists
    """
    if not allowed_dirs:
        return True  # No restriction - allow everything

    path = urlparse(url).path.lower()

    # Check if path starts with any of the allowed directories
    for d in allowed_dirs:
        d = (d or "").strip("/").lower()
        if not d:
            continue
        # Match URLs like /news/article123 or /world/breaking-news
        if path.startswith(f"/{d}/") or path == f"/{d}":
            return True

    return False


def polite_get(session: requests.Session, url: str) -> requests.Response:
    time.sleep(SLEEP_BASE_SEC + random.random()*0.6)
    resp = session.get(url, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    return resp

def _origin(u: str) -> str:
    p = urlsplit(u); return urlunsplit((p.scheme or "https", p.netloc, "/", "", ""))
def host_variants(u: str):
    p = urlsplit(u); scheme = p.scheme or "https"
    ext = tldextract.extract(p.netloc)
    base = f"{ext.domain}.{ext.suffix}"
    cands = [f"{scheme}://{p.netloc}/", f"{scheme}://www.{base}/", f"{scheme}://{base}/"]
    seen=set(); out=[]; [out.append(c) for c in cands if not (c in seen or seen.add(c))]
    return out
def pick_accessible_origin(session, site_url):
    for o in host_variants(site_url):
        try:
            r = session.get(o, timeout=(3,5), allow_redirects=True)
            if _VERBOSE: logger.info(f"[probe] {o} -> {r.status_code}")
            if r.status_code < 400 and r.content:
                return o.rstrip("/")
        except Exception: pass
    return _origin(site_url).rstrip("/")

# ------------------ Discovery via robots sitemaps ------------------
def peek_sitemap_head(resp) -> bytes:
    # If it’s gzipped (by URL or magic bytes), decompress a small slice
    content = resp.content
    if resp.url.lower().endswith(".gz") or (len(content) >= 2 and content[0] == 0x1F and content[1] == 0x8B):
        try:
            return gzip.GzipFile(fileobj=io.BytesIO(content)).read(800).lower()
        except Exception:
            return b""
    return content[:800].lower()

def is_valid_sitemap(resp) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    head = peek_sitemap_head(resp)

    # hard-fail if it’s clearly HTML
    if "html" in ct or b"<html" in head:
        return False

    # allow xml-ish or generic types; some sites lie (octet-stream)
    # accept when bytes contain sitemap markers
    return (b"<urlset" in head) or (b"<sitemapindex" in head)

def discover_sitemaps(session, site_url):
    from urllib.parse import urljoin
    base = site_url.rstrip('/') + '/'
    robots_url = urljoin(base, "robots.txt")
    sitemaps = []

    # --- don't raise on robots.txt errors ---
    try:
        r = session.get(robots_url, timeout=(3,5), allow_redirects=True)
        if _VERBOSE: logger.info(f"[robots] {robots_url} status={r.status_code}")
        if r.status_code == 200 and r.text:
            for raw_line in r.text.splitlines():
                line = raw_line.strip()
                if not line.lower().startswith("sitemap:"):
                    continue
                # take the value after "sitemap:"
                val = line.split(":", 1)[1]
                # drop inline comments like "# ..." or "; ..."
                val = val.split("#", 1)[0].split(";", 1)[0].strip()
                if not val:
                    continue
                # allow relative sitemap paths
                #from urllib.parse import urljoin, urlparse
                sm_url = val if urlparse(val).scheme else urljoin(base, val)
                sitemaps.append(sm_url)
    except Exception as e:
        if _VERBOSE: logger.info(f"[robots] ignored error {robots_url}: {e}")
    # ---------------------------------------
    # Fallback guesses ALWAYS attempted (even if robots failed)
    if not sitemaps:
        guesses = [
            "sitemap.xml", "sitemap_index.xml", "sitemap-news.xml",
            "sitemap/sitemap.xml", "sitemap-index.xml", "wp-sitemap.xml",
            "sitemap.xml.gz", "sitemap_index.xml.gz",
        ]

        # --- NEW: try both www and non-www bases ---
        parsed = urlparse(site_url)
        bases = [base]
        if parsed.netloc.startswith("www."):
            bases.append(base.replace("www.", "", 1))
        # -------------------------------------------

        try:
            old_headers = session.headers.copy()
            session.headers.clear()
            session.headers.update(HTML_HEADERS) # appear more human
            for b in bases:
                for g in guesses:
                    #print(urljoin(b, g))    
                    url = urljoin(b, g)
                    try:
                        # quick probe with HEAD (cheap + polite)
                        time.sleep(0.1 + random.random()*0.2)
                        rh = session.head(url, timeout=(3, 5), allow_redirects=True)

                        # if HEAD is clearly bad, skip; but still allow common cases:
                        # - 200 OK
                        # - redirects (we'll follow on GET)
                        # - 405 Method Not Allowed (some servers disallow HEAD)
                        if rh.status_code not in (200, 301, 302, 307, 308, 405):
                            continue
                        rr = session.get(url, timeout=(3, 5), allow_redirects=True)
                        if rr.status_code != 200 or not rr.content:
                            continue

                        if not is_valid_sitemap(rr):
                            if _VERBOSE: logger.info(f"[robots] not a real sitemap {url}")
                            continue

                        # --- Valid sitemap ---
                        sitemaps.append(url)
                        if _VERBOSE: logger.info(f"[robots] valid sitemap hit {url}")
                    except Exception:
                        pass
        finally:
            session.headers.clear()
            session.headers.update(old_headers) # revert back simple headers for fetching

    if _VERBOSE: logger.info(f"[robots] found {len(sitemaps)} sitemap entries")
    return sitemaps

def fetch_sitemap_urls(session: requests.Session, sitemap_url: str) -> Tuple[
    List[Tuple[str, Optional[datetime], Optional[str]]],
    List[Tuple[str, Optional[datetime]]]
]:
    """
    Returns (url_entries, nested_sitemaps)
    url_entries: list of (loc, lastmod/publish_date, news_title)
    nested_sitemaps: list of (sitemap_url, lastmod) found in index
    """
    try:
        r = polite_get(session, sitemap_url)
        r.raise_for_status()

        # gzip handling goes here (still inside this try in fetch_sitemap_urls)
        ct = (r.headers.get("Content-Type") or "").lower()
        ce = (r.headers.get("Content-Encoding") or "").lower()
        raw = r.content

        if sitemap_url.endswith(".gz") or "gzip" in ct or raw[:2] == b"\x1f\x8b":
            if "gzip" not in ce:
                import gzip
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass

        if _VERBOSE: logger.info(f"[sitemap] GET {sitemap_url} -> {r.status_code} {len(r.content)} bytes")
    except Exception as e:
        if _VERBOSE: logger.info(f"[sitemap] failed {sitemap_url}: {e}")
        return ([], [])

    # --- ONLY NEW GUARD: skip HTML pretending to be sitemap ---
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype or (r.content[:200].lower().find(b"<html") != -1):
        if _VERBOSE: logger.info(f"[sitemap] looks like HTML/WAF at {sitemap_url} (ctype={ctype}), skipping")
        return ([], [])
    # -----------------------------------------------------------

    # Use `raw` (gzip-decompressed when applicable), NOT r.content — for .gz
    # sitemaps r.content is still compressed and would fail XML parsing.
    content = raw
    try:
        root = etree.fromstring(content)
    except Exception:
        parser = etree.XMLParser(recover=True, huge_tree=True)
        try:
            root = etree.fromstring(content, parser=parser)
        except Exception as e:
            if _VERBOSE: logger.info(f"[sitemap] XML parse error {sitemap_url}: {e}")
            return ([], [])

    if root is None:
        if _VERBOSE: logger.info(f"[sitemap] XML parse returned None for {sitemap_url}")
        return ([], [])

    # Detect actual namespace from root (some sites use https:// instead of http://)
    root_ns = root.nsmap.get(None, "http://www.sitemaps.org/schemas/sitemap/0.9")
    news_ns = next((v for v in root.nsmap.values() if "sitemap-news" in v), "http://www.google.com/schemas/sitemap-news/0.9")
    ns = {"sm": root_ns, "news": news_ns}

    url_entries: List[Tuple[str, Optional[datetime], Optional[str]]] = []
    nested: List[Tuple[str, Optional[datetime]]] = []

    # Sitemap index
    for sm_el in root.findall(".//sm:sitemap", namespaces=ns):
        loc_el = sm_el.find("sm:loc", namespaces=ns)
        last_el = sm_el.find("sm:lastmod", namespaces=ns)
        if loc_el is not None and loc_el.text:
            loc = loc_el.text.strip()
            lmod = parse_dt(last_el.text.strip()) if (last_el is not None and last_el.text) else None
            nested.append((loc, lmod))

    # URL set
    for url_el in root.findall(".//sm:url", namespaces=ns):
        loc_el = url_el.find("sm:loc", namespaces=ns)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        lastmod_el = url_el.find("sm:lastmod", namespaces=ns)
        lastmod = parse_dt(lastmod_el.text.strip()) if lastmod_el is not None and lastmod_el.text else None
        # News sitemap extras
        news_title_el = url_el.find("news:news/news:title", namespaces=ns)
        pub_date_el = url_el.find("news:news/news:publication_date", namespaces=ns)
        pub_dt = parse_dt(pub_date_el.text.strip()) if pub_date_el is not None and pub_date_el.text else None
        title = news_title_el.text.strip() if news_title_el is not None and news_title_el.text else None
        url_entries.append((loc, pub_dt or lastmod, title))

    if _VERBOSE: logger.info(f"[sitemap] items={len(url_entries)} nested_indexes={len(nested)} -> {sitemap_url}")

    return (url_entries, nested)

def collect_from_sitemaps(session: requests.Session, site_url: str, start: datetime, end: datetime, *, max_per_source: int=5000) -> List[ArticleHint]:
    hints: List[ArticleHint] = []
    seen_sitemaps: Set[str] = set()

    # Get allowed_dirs for filtering (if specified) - used for RSS/frontpage, not sitemaps
    rules = sources.get_site_rules(site_url)
    allowed_dirs = rules.get("allowed_dirs") if rules else None
    
    raw = discover_sitemaps(session, site_url)
    queue: List[Tuple[str, Optional[datetime]]] = [(u, None) for u in raw]

    grace = timedelta(days=7)

    def _sitemap_date_hint(url: str) -> Optional[datetime]:
        """
        Try to infer a date from sitemap URL patterns like:
        .../sitemap-2025-10.xml          → 2025-10 (use end-of-month)
        .../sitemap-2025-10-20.xml       → 2025-10-20
        .../sitemap-2026-03_1.xml        → page-numbered monthly: use end-of-month
        .../sitemap-index-202605.xml     → 2026-05 compact YYYYMM (use end-of-month)
        .../sitemap-20260520.xml         → 2026-05-20 compact YYYYMMDD
        If the third component is separated by '_', treat it as a page number
        (not a day) and use end-of-month so the sitemap isn't pruned too early.
        """
        import calendar

        def _build(y: int, mo: int, da: Optional[int]) -> Optional[datetime]:
            if mo < 1: mo = 1
            if mo > 12: mo = 12
            try:
                last_day = calendar.monthrange(y, mo)[1]
            except Exception:
                last_day = 28
            if da is None:
                da = last_day
            else:
                if da < 1: da = 1
                if da > last_day: da = last_day
            try:
                return datetime(y, mo, da, tzinfo=BERLIN_TZ)
            except Exception:
                try:
                    return datetime(y, 1, 1, tzinfo=BERLIN_TZ)
                except Exception:
                    return None

        # Compact form: YYYYMM or YYYYMMDD with NO separators
        # (e.g. bild's sitemap-index-202605.xml). Bounded by \d so we don't
        # slice a date out of the middle of a longer numeric id.
        cm = re.search(r'(?<!\d)(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})?(?!\d)', url)
        if cm:
            mo = int(cm.group('m'))
            if 1 <= mo <= 12:
                da = int(cm.group('d')) if cm.group('d') else None
                return _build(int(cm.group('y')), mo, da)

        # Separator form: YYYY-MM, YYYY-MM-DD, YYYY-MM_page
        m = re.search(
            r'(?P<y>(?:19|20)\d{2})'
            r'(?:[-_/](?P<m>\d{1,2})'
            r'(?:(?P<sep2>[-_/])(?P<d>\d{1,2}))?)?',
            url
        )
        if not m:
            return None

        y = int(m.group('y'))
        mo = int(m.group('m')) if m.group('m') else 1
        sep2 = m.group('sep2')
        raw_d = int(m.group('d')) if m.group('d') else None

        # If the separator before the third component is '_', it's a page number
        # (e.g. sitemap-2026-03_1.xml). Use end-of-month so we don't prune it.
        da = None if (sep2 == '_' or raw_d is None) else raw_d
        return _build(y, mo, da)

    # Get allowed_dirs for filtering (if specified) - moved up before discover_sitemaps
    # (already extracted above, removed duplicate code)

    per_source_counts: Dict[str, int] = {"sitemap": 0}
    max_sitemap_fetches = 100  # guards against broken/infinite sitemap trees; skips don't count
    sitemap_fetch_count = 0
    while queue:
        if sitemap_fetch_count >= max_sitemap_fetches:
            if _VERBOSE: logger.info(f"[sitemap] reached max_sitemap_fetches={max_sitemap_fetches}, stopping traversal")
            break
        sm, sm_lastmod = queue.pop(0)
        if sm in seen_sitemaps:
            continue
        seen_sitemaps.add(sm)

        # Skip category/section sitemaps — they contain landing pages, not articles
        if "sitemap-category" in sm:
            if _VERBOSE: logger.info(f"[sitemap] skipping category sitemap {sm}")
            continue

        hint_dt = sm_lastmod or _sitemap_date_hint(sm)
        if hint_dt and hint_dt < (start - grace):
            if _VERBOSE: logger.info(f"[sitemap] prune old index {sm} (hint {hint_dt.isoformat()})")
            continue

        url_entries, nested = fetch_sitemap_urls(session, sm)
        sitemap_fetch_count += 1

        for loc, dt, title in url_entries:
            entry_dt = dt or hint_dt
            if entry_dt and (start <= entry_dt <= end):
                # Filter by allowed_dirs if specified
                if not url_matches_dirs(loc, allowed_dirs):
                    continue
                if per_source_counts["sitemap"] < max_per_source:
                    hints.append(ArticleHint(url=normalize_url(loc), published_at=entry_dt, title=title, source="sitemap"))
                    per_source_counts["sitemap"] += 1

        # Sort nested sitemaps newest-first before inserting so that sites with
        # massive historical archives (e.g. zeit.de goes back to 1946) don't
        # exhaust the timeout before reaching recent months.
        nested_sorted = sorted(
            nested,
            key=lambda x: x[1] or _sitemap_date_hint(x[0]) or datetime.min.replace(tzinfo=BERLIN_TZ),
            reverse=True,
        )
        queue = nested_sorted + queue

        if per_source_counts["sitemap"] >= max_per_source:
            if _VERBOSE: logger.info(f"[sitemap] reached max_per_source={max_per_source}, stopping traversal")
            break

    if _VERBOSE: logger.info(f"[sitemap] total hints: {len(hints)}")
    return hints

# ------------------ Discovery via RSS/Atom ------------------
def collect_from_feeds(session, site_url):
    """
    Discover article URLs via RSS/Atom without any homepage HTML parsing.
      - Tries common on-site feed paths (COMMON_FEED_PATHS)
    Returns: List[ArticleHint]
    """
    from urllib.parse import urljoin, urlsplit
    import feedparser

    hints: List[ArticleHint] = []
    base = site_url.rstrip('/')

    feed_urls: List[str] = [urljoin(base + '/', p) for p in COMMON_FEED_PATHS]

    # De-dup in order
    feed_urls = list(dict.fromkeys(feed_urls))
    if _VERBOSE: logger.info(f"[rss] candidates={len(feed_urls)}")

    # Fetch & parse feeds (no HTML discovery here)
    netloc = urlsplit(base).netloc
    if tldextract:
        ext = tldextract.extract(netloc)
        root_reg = f"{ext.domain}.{ext.suffix}" if ext.suffix else netloc
    else:
        root_reg = netloc.removeprefix("www.")

    # Fetch & parse feeds (RSS = XML, not HTML - no Playwright needed)
    for fu in feed_urls:
        try:
            raw = fetch_html(session, fu, timeout=(5, 8), use_playwright_fallback=False)
            if not raw:
                if _VERBOSE: logger.info(f"[rss] miss {fu}: empty")
                continue

            parsed = feedparser.parse(raw)
            entries = getattr(parsed, "entries", []) or []
            if _VERBOSE: logger.info(f"[rss] {fu} entries={len(entries)}")

            for en in entries:
                url = (en.get("link") or en.get("id") or "").strip()
                if not url:
                    continue

                # --- keep only same registrable domain (e.g., *.ui.ac.id) ---
                try:
                    host = (urlsplit(url).netloc or "").lower()
                    if not host:
                        continue
                    ext = tldextract.extract(host)
                    reg = f"{ext.domain}.{ext.suffix}" if ext.suffix else host
                    if reg != root_reg:
                        continue
                except Exception:
                    continue

                url = normalize_url(url)

                title = en.get("title") or None

                # published time (best-effort)
                dt_raw = en.get("published") or en.get("updated") or en.get("created")
                published = None
                if dt_raw:
                    published = parse_dt(dt_raw)
                if not published:
                    # feedparser gives struct_time; convert safely
                    for key in ("published_parsed", "updated_parsed", "created_parsed"):
                        t = en.get(key)
                        if t:
                            try:
                                import time as _t, datetime as _dt
                                ts = _t.mktime(t) if hasattr(t, "tm_year") else None
                                if ts:
                                    published = _dt.datetime.fromtimestamp(ts, tz=BERLIN_TZ)
                                    break
                            except Exception:
                                continue
                hints.append(ArticleHint(
                    url=url,
                    published_at=published,
                    title=title,
                    source="rss",
                ))

        except Exception as ex:
            if _VERBOSE: logger.info(f"[rss] error {fu}: {ex}")

    return hints

# ------------------ Discovery from Frontpage ------------------
def _extract_date_near_link(link_elem):
    """
    Try to find a date near an article link in the HTML.
    Looks in parent/sibling elements for common date patterns.
    Returns datetime or None.
    """
    from .crawler_html_utils import parse_flexible

    # Try to find dates in nearby elements (parent, siblings)
    # Common patterns: date in parent div, sibling span/time, etc.
    candidates = []

    # Check parent and grandparent elements
    parent = link_elem.getparent()
    if parent is not None:
        # Check siblings first (common pattern: <a>title</a><p>date</p>)
        for sibling in parent.xpath('./*'):
            if sibling == link_elem:
                continue
            # Check <p>, <span>, <div> siblings that might contain dates
            if sibling.tag in ('p', 'span', 'div', 'time'):
                sibling_text = sibling.text_content().strip()
                if len(sibling_text) < 200:  # Skip very long text
                    candidates.append(sibling_text)
                # Also check datetime attribute if it's a time element
                if sibling.tag == 'time':
                    dt_attr = sibling.get('datetime')
                    if dt_attr:
                        candidates.append(dt_attr)

        # Get all text from parent
        parent_text = ' '.join(parent.xpath('.//text()'))
        candidates.append(parent_text)

        # Check for time elements in parent
        for time_elem in parent.xpath('.//time'):
            dt_attr = time_elem.get('datetime')
            if dt_attr:
                candidates.append(dt_attr)
            candidates.append(time_elem.text_content())

        # Check for date classes in parent
        for date_elem in parent.xpath('.//*[contains(@class, "date") or contains(@class, "time")]'):
            candidates.append(date_elem.text_content())

        # Check grandparent too
        grandparent = parent.getparent()
        if grandparent is not None:
            for time_elem in grandparent.xpath('.//time'):
                dt_attr = time_elem.get('datetime')
                if dt_attr:
                    candidates.append(dt_attr)

    # Try to parse each candidate
    for candidate in candidates:
        if not candidate or not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if len(candidate) > 200:  # Skip overly long text
            continue
        dt = parse_flexible(candidate)
        if dt:
            return dt

    return None

def collect_from_frontpage(session, site_url, start_date=None, cap=80):
    """
    Scrape homepage + common sections for article anchors.
    All fetches go through crawler_html_utils.fetch_html() so Playwright/reader-proxy fallbacks can apply.
    Now extracts dates from HTML to avoid expensive later scraping.

    start_date: If provided, dates found < start_date are kept (to filter out later),
                dates >= start_date are discarded (to force fetch for exact datetime).

    Returns: List[ArticleHint]
    """
    from urllib.parse import urljoin, urlparse
    from lxml import html as _lxml_html
    import re as _re  # <— local alias so a global 're = None' won't break us

    # --- Get allowed_dirs from config ---
    rules = sources.get_site_rules(site_url)
    dirs = rules.get("allowed_dirs") if rules else None  # e.g. ["", "world", "business"]

    if dirs:
        base_root = site_url.rstrip('/')
        bases = []
        for d in dirs:
            d = (d or "").strip("/")
            # Don't add trailing slash - let the site handle it naturally
            bases.append(base_root if not d else urljoin(base_root + '/', d))
        # de-dup keeping order
        seen_b = set(); bases = [b for b in bases if not (b in seen_b or seen_b.add(b))]
    else:
        # Don't add trailing slashes - many sites treat /section and /section/ differently
        base_root = site_url.rstrip('/')
        bases = [
            base_root,
            urljoin(base_root + '/', 'news'),
            urljoin(base_root + '/', 'world'),
            urljoin(base_root + '/', 'business'),
            urljoin(base_root + '/', 'technology'),
        ]
        # in-order de-dup
        seen_b = set(); bases = [b for b in bases if not (b in seen_b or seen_b.add(b))]

    # Track URLs with their dates: {url: (datetime|None)}
    url_dates = {}
    netloc_root = urlparse(site_url).netloc

    # Check if brightdata should be used
    use_brightdata = sources.is_brightdata_enabled(site_url)

    # Remember the base URLs we're scraping so we can exclude them from results
    # (we want articles FROM these pages, not the pages themselves)
    base_urls_to_exclude = set(normalize_url(b) for b in bases)

    for b in bases:
        try:
            data = fetch_html(session, b, timeout=(5, 8), use_brightdata=use_brightdata)
            if not data:
                continue

            # Try HTML parsing first
            link_elements = []
            try:
                # Decode bytes first - lxml.html.fromstring(bytes) ignores meta charset
                html_str = data.decode('utf-8', 'ignore') if isinstance(data, bytes) else data
                doc = _lxml_html.fromstring(html_str)
                # Get link elements (not just hrefs) so we can extract dates
                link_elements = doc.xpath('//a[@href]')
            except Exception:
                # Plain-text fallback (e.g., reader-proxy output)
                # Can't extract dates from plain text, just get URLs
                text = (
                    data.decode("utf-8", "ignore")
                    if isinstance(data, (bytes, bytearray))
                    else str(data or "")
                )
                hrefs = _re.findall(r'https?://[^\s)>"\']+', text)
                for href in hrefs:
                    u = urljoin(b, href)
                    if u not in url_dates:
                        url_dates[u] = None  # No date info from plain text

            for link_elem in link_elements:
                href = (link_elem.get('href') or '').strip()
                if not href:
                    continue

                u = urljoin(b, href)
                pu = urlparse(u)
                if not pu.netloc or not pu.netloc.endswith(netloc_root):
                    continue

                # Exclude the base URLs themselves (list pages we're scraping)
                # We want articles FROM these pages, not the pages themselves
                u_normalized = normalize_url(u)
                if u_normalized in base_urls_to_exclude:
                    continue

                # REMOVED: path_hints filtering (too restrictive for SPAs and sites with complex routing)
                # if not any(seg in pu.path for seg in path_hints):
                #     continue

                # Exclude common non-article patterns
                excluded_path_patterns = [
                    '/page/', '/category/', '/tag/', '/author/', '/list',
                    '/cdn-cgi/',  # Cloudflare protection pages
                    '/entertainment', '/lifestyle', '/sports', '/sport',
                    '/horoscope', '/photos',
                    '/faqs', '/aboutus', '/contactus', '/authors',
                ]
                if any(pattern in pu.path.lower() for pattern in excluded_path_patterns):
                    continue

                # Exclude query-based pagination (e.g., ?page=2)
                if 'page=' in pu.query:
                    continue

                # Exclude bare directory pages (common list page endpoints)
                # e.g., /news, /reviews, /announcement, /publikasi, /press-release
                # but allow /news/12345/article-title (has content after the directory)
                path_stripped = pu.path.rstrip('/').lower()
                bare_directory_names = [
                    '/list', '/articles', '/archive',
                    '/news', '/announcement', '/announcements', '/reviews',
                    '/press-release', '/press-releases',
                    '/gallery', '/press-conference',
                    '/search', '/pages',
                    '/world', '/business', '/technology',
                    '/terms', '/schedule', '/regions', '/factcheck',
                    '/financial', '/politics', '/opinion', '/national',
                    '/education', '/tech', '/food', '/latest', '/focus',
                    '/letters', '/columnists'
                ]
                if any(path_stripped.endswith(pattern) for pattern in bare_directory_names):
                    continue

                # Extract date (even if URL seen before - might find date in a different location)
                if u not in url_dates:
                    url_dates[u] = None  # Initialize

                # Try to extract date if we don't have one yet
                if url_dates[u] is None:
                    date_found = _extract_date_near_link(link_elem)
                    if date_found:
                        # Make date optimistic (23:59:59) to err on side of inclusion
                        date_optimistic = date_found.replace(hour=23, minute=59, second=59)

                        # Only keep the date if it's OUTSIDE the target range (to filter out later)
                        # If it's inside range, leave as None to force fetch for exact datetime
                        if start_date and date_optimistic >= start_date:
                            # In range -> don't store, will fetch exact time later
                            if _VERBOSE: logger.info(f"[front] date {date_found.date()} for {u} in range, will fetch exact time")
                        else:
                            # Too old (or no start_date) -> keep it to filter out later
                            url_dates[u] = date_optimistic
                            if _VERBOSE: logger.info(f"[front] extracted date for {u}: {date_found.date()} (set to 23:59:59)")

            if _VERBOSE: logger.info(f"[front] scanned {b} -> found {len(url_dates)} so far")

            # Don't break early - scan all configured allowed_dirs to get comprehensive coverage
            # The cap will be applied when converting to ArticleHints (line 970)

        except Exception as e:
            if _VERBOSE: logger.info(f"[front] failed {b}: {e}")

    # Convert url_dates dict to list of ArticleHints
    selected_items = list(url_dates.items())[:cap]
    dates_found = sum(1 for url, dt in selected_items if dt is not None)

    if _VERBOSE: logger.info(f"[front] collected ~{len(selected_items)} candidate links, {dates_found} with dates extracted")


    return [
        ArticleHint(url=url, published_at=dt, title=None, source="frontpage")
        for url, dt in selected_items
    ]

def crawl_site(site_url: str, start: datetime, end: datetime, max_per_source: int = 5000) -> tuple[List[ArticleRecord], int]:

    # Load blacklist URLs from config (cached, filtered to this specific source)
    blacklist_urls = sources.load_blacklist(source_url=site_url)
    if _VERBOSE: logger.info(f"[blacklist] loaded {len(blacklist_urls)} URLs for {domain_of(site_url)}")

    # Check if brightdata should be used (once for entire crawl)
    use_brightdata = sources.is_brightdata_enabled(site_url)

    session = requests.Session()
    session.headers.update(HTML_HEADERS) #real person header
    origin = pick_accessible_origin(session, site_url)
    session.headers["Referer"] = origin + "/"   # helps some WAFs
    if _VERBOSE and origin.rstrip('/') != _origin(site_url).rstrip('/'): logger.info(f"[probe] using origin {origin}")

    session_for_sm = requests.Session()
    session_for_sm.headers.update(SITEMAP_HEADERS) #Sidemaps need simple robot headers

    hints = []

    # If Bright Data is enabled, use it exclusively (other methods will likely fail due to anti-bot protection)
    if use_brightdata:
        if _VERBOSE: logger.info("[brightdata] Expensive brightdata crawl for " + site_url)
        hints += collect_from_frontpage(session, origin, start_date=start, cap=80)
    else:
        if feature_allowed(site_url, "sitemap"):
            sitemap_hints = []
            for attempt in range(3):  # Try up to 3 times
                sitemap_hints = collect_from_sitemaps(session_for_sm, site_url, start, end, max_per_source=max_per_source)
                if len(sitemap_hints) > 0:
                    break
            hints += sitemap_hints
        if feature_allowed(site_url, "feeds"):
            hints += collect_from_feeds(session, origin)
        if feature_allowed(site_url, "frontpage"):
            hints += collect_from_frontpage(session, origin, start_date=start, cap=300)

    if _VERBOSE: logger.info(f"[hints] total={len(hints)}")

    dated = [h for h in hints if h.published_at]
    undated = [h for h in hints if not h.published_at]
    undated_enriched = enrich_dates_light(session, undated, cap=300, workers=12, site_url=site_url, use_brightdata=use_brightdata)
    candidate_hints = dated + undated_enriched

    # then filter by date range
    by_source = {"sitemap": 0, "rss": 0, "frontpage": 0}
    filtered = []
    for h in candidate_hints:
        if h.published_at and in_range(h.published_at, start, end):
            if by_source.get(h.source, 0) < max_per_source:
                filtered.append(h)
                by_source[h.source] = by_source.get(h.source, 0) + 1

    if _VERBOSE: logger.info(f"[filter] kept={len(filtered)} by_source={by_source}")

    seen: Set[str] = set()
    uniq: List[ArticleHint] = []
    for h in filtered:
        u = normalize_url(h.url)
        if u in seen:
            continue
        seen.add(u)
        uniq.append(dataclasses.replace(h, url=u))

    if _VERBOSE: logger.info(f"[dedup] uniq={len(uniq)}")

    records: List[ArticleRecord] = []
    fetch_title_count = 0
    site = domain_of(site_url)
    crawl_ts = now_berlin_iso()

    ordered = sorted(uniq, key=lambda x: x.published_at or datetime.min.replace(tzinfo=BERLIN_TZ))

    # News sitemaps only carry titles for ~48h; older entries come from the plain
    # sitemaps with no title, so each must be fetched one-by-one (the slow part).
    need_fetch = sum(1 for h in ordered
                     if not h.title and normalize_url(h.url) not in blacklist_urls)
    have_title = sum(1 for h in ordered if h.title)
    if need_fetch:
        logger.info(f"[{site}] {len(ordered)} articles in window — {have_title} already titled, "
                    f"{need_fetch} need a page-open to read the title (fetching one-by-one)...")
    else:
        logger.info(f"[{site}] {len(ordered)} articles in window — all already titled, no fetching needed")

    fetched_so_far = 0
    for h in ordered:
        rec = ArticleRecord(
            site=site,
            title=h.title,
            url=h.url,
            published_at=h.published_at.isoformat() if h.published_at else None,
            crawled_at=crawl_ts,
        )

        # Check blacklist before fetching title
        if normalize_url(rec.url) in blacklist_urls:
            if _VERBOSE: logger.info(f"[blacklist] skipping {rec.url}")
            continue

        if not rec.title:
            fetched_so_far += 1
            if fetched_so_far == 1 or fetched_so_far % 50 == 0 or fetched_so_far == need_fetch:
                logger.info(f"[{site}] fetching titles {fetched_so_far}/{need_fetch}...")
            time.sleep(0.4)
            rec.title = fetch_title_fallback(session, rec.url, use_brightdata=use_brightdata)
            if rec.title:
                fetch_title_count += 1

        if rec.title: #It makes no sense to add records without title???? Not assessable?
            if _VERBOSE: logger.info(f"[fetch] fetched {rec.url}")
            records.append(rec)
        else:
            if _VERBOSE: logger.info(f"[fetch] No title, not fetched {rec.url}")

    logger.info(f"[{site}] done — {len(records)} articles with titles ({fetch_title_count} fetched from page)")
    return records, fetch_title_count

