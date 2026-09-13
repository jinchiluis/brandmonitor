# utilities.py
import json, re, os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from lxml import html, etree
from .crawler_playwright import fetch_html_with_playwright
from bs4 import BeautifulSoup
from typing import Optional
from src.logger import get_logger

logger = get_logger(__name__)

from src.config import CRAWLER_VERBOSE as _VERBOSE

# --------------------- flexible date parser ---------------------
MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January","February","March","April","May","June",
     "July","August","September","October","November","December"]
)}
# Add abbreviated forms
MONTHS.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12
})
# Add Indonesian month names (for kemlu.go.id and other Indonesian sites)
MONTHS.update({
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12
})
# Add Thai month names (for Thai government sites)
MONTHS.update({
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    # Abbreviated forms
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12
})
# Add Khmer month names (for Cambodian sites)
MONTHS.update({
    "មករា": 1, "កុម្ភៈ": 2, "មីនា": 3, "មេសា": 4, "ឧសភា": 5, "មិថុនា": 6,
    "កក្កដា": 7, "សីហា": 8, "កញ្ញា": 9, "តុលា": 10, "វិច្ឆិកា": 11, "ធ្នូ": 12
})

ISO_PAT = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?')
MDY_PAT = re.compile(
    r'(?P<mon>[A-Za-z]+)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})'
    r'(?:(?:\s*[|,\-]\s*|\s+)(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?::(?P<sec>\d{2}))?\s*(?P<ampm>am|pm|AM|PM)?)?'
)
# Indonesian/European/Thai day-first format: "28 Oktober 2025" or "28 มกราคม 2568"
DMY_PAT = re.compile(
    r'(?P<day>\d{1,2})\s+(?P<mon>[^\s\d]+)\s+(?P<year>\d{4})'
    r'(?:(?:\s*[|,\-]\s*|\s+)(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?::(?P<sec>\d{2}))?\s*(?P<ampm>am|pm|AM|PM)?)?'
)
DATE_ONLY_PAT = re.compile(r'(?P<year>\d{4})[-/\.](?P<mon>\d{1,2})[-/\.](?P<day>\d{1,2})')
# European/Asian DD/MM/YYYY format (for kpl.gov.la and similar sites): "26/01/2026 13:25"
DMY_NUMERIC_PAT = re.compile(
    r'(?P<day>\d{1,2})[-/\.](?P<mon>\d{1,2})[-/\.](?P<year>\d{4})'
    r'(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<sec>\d{2}))?)?'
)
# Chinese numeric format: "11月 14, 2025" or "2025年11月14日"
CHINESE_MDY_PAT = re.compile(r'(?P<mon>\d{1,2})月\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})')
CHINESE_YMD_PAT = re.compile(r'(?P<year>\d{4})年\s*(?P<mon>\d{1,2})月\s*(?P<day>\d{1,2})日?')
# Khmer format: "៣ សីហា ២០២៥" (day month year) - supports both Khmer and Arabic numerals
KHMER_DMY_PAT = re.compile(r'(?P<day>[០-៩\d]{1,2})\s+(?P<mon>[^\s\d]+)\s+(?P<year>[០-៩\d]{4})')

# Khmer numeral conversion
KHMER_NUMERALS = str.maketrans('០១២៣៤៥៦៧៨៩', '0123456789')

def _khmer_to_arabic(s: str) -> str:
    """Convert Khmer numerals to Arabic numerals."""
    return s.translate(KHMER_NUMERALS)

def _convert_buddhist_year(year: int) -> int:
    """Convert Buddhist Era year to Gregorian year if needed."""
    # Buddhist Era years are typically 543 years ahead (e.g., 2568 BE = 2025 CE)
    # Only convert if year is > 2200 (safely assume it's BE)
    if year > 2200:
        return year - 543
    return year

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def parse_flexible(s: str):
    if not s: return None
    s = s.strip()

    m = ISO_PAT.search(s)
    if m:
        try:
            return _aware(datetime.fromisoformat(m.group(0).replace("Z", "+00:00")))
        except Exception:
            pass

    m = MDY_PAT.search(s)
    if m:
        try:
            mon = MONTHS.get(m.group("mon").lower())
            day = int(m.group("day")); year = _convert_buddhist_year(int(m.group("year")))
            hour = int(m.group("hour")) if m.group("hour") else 0
            minute = int(m.group("minute") or 0)
            sec = int(m.group("sec") or 0)
            ampm = (m.group("ampm") or "").lower()
            if ampm == "pm" and hour < 12: hour += 12
            if ampm == "am" and hour == 12: hour = 0
            return _aware(datetime(year, mon, day, hour, minute, sec))
        except Exception:
            pass

    # Try DMY format (Indonesian/European/Thai): "28 Oktober 2025" or "28 มกราคม 2568"
    m = DMY_PAT.search(s)
    if m:
        try:
            mon = MONTHS.get(m.group("mon").lower())
            day = int(m.group("day")); year = _convert_buddhist_year(int(m.group("year")))
            hour = int(m.group("hour")) if m.group("hour") else 0
            minute = int(m.group("minute") or 0)
            sec = int(m.group("sec") or 0)
            ampm = (m.group("ampm") or "").lower()
            if ampm == "pm" and hour < 12: hour += 12
            if ampm == "am" and hour == 12: hour = 0
            return _aware(datetime(year, mon, day, hour, minute, sec))
        except Exception:
            pass

    m = DATE_ONLY_PAT.search(s)
    if m:
        try:
            year = _convert_buddhist_year(int(m.group("year"))); mon = int(m.group("mon")); day = int(m.group("day"))
            return _aware(datetime(year, mon, day, 12, 0, 0))
        except Exception:
            pass

    # European/Asian DD/MM/YYYY format (for kpl.gov.la): "26/01/2026 13:25"
    m = DMY_NUMERIC_PAT.search(s)
    if m:
        try:
            day = int(m.group("day")); mon = int(m.group("mon")); year = _convert_buddhist_year(int(m.group("year")))
            hour = int(m.group("hour")) if m.group("hour") else 12
            minute = int(m.group("minute")) if m.group("minute") else 0
            sec = int(m.group("sec") or 0)
            return _aware(datetime(year, mon, day, hour, minute, sec))
        except Exception:
            pass

    # Chinese numeric format: "11月 14, 2025"
    m = CHINESE_MDY_PAT.search(s)
    if m:
        try:
            mon = int(m.group("mon")); day = int(m.group("day")); year = _convert_buddhist_year(int(m.group("year")))
            return _aware(datetime(year, mon, day, 12, 0, 0))
        except Exception:
            pass

    # Chinese YMD format: "2025年11月14日"
    m = CHINESE_YMD_PAT.search(s)
    if m:
        try:
            year = _convert_buddhist_year(int(m.group("year"))); mon = int(m.group("mon")); day = int(m.group("day"))
            return _aware(datetime(year, mon, day, 12, 0, 0))
        except Exception:
            pass

    # Khmer format: "៣ សីហា ២០២៥" (3 August 2025)
    m = KHMER_DMY_PAT.search(s)
    if m:
        try:
            # Convert Khmer numerals to Arabic
            day_str = _khmer_to_arabic(m.group("day"))
            year_str = _khmer_to_arabic(m.group("year"))
            mon_name = m.group("mon").strip()

            # Look up month name
            mon = MONTHS.get(mon_name.lower())
            if mon:
                day = int(day_str)
                year = _convert_buddhist_year(int(year_str))
                return _aware(datetime(year, mon, day, 12, 0, 0))
        except Exception:
            pass

    return None

def fetch_html(session, url, timeout=(3,5), use_playwright_fallback=True, use_brightdata=False):
    """
    Fetch HTML with optional Playwright or Bright Data fallback on 403/errors or SPA shells.

    Args:
        use_playwright_fallback: If False, just return empty on errors (useful for RSS feeds)
        use_brightdata: If True, use Bright Data API instead of Playwright for fallback
    """
    # If brightdata is explicitly requested, use it directly
    if use_brightdata:
        if _VERBOSE: logger.info(f"[fetch_html] using Bright Data for {url}")
        from .crawler_brightdata import fetch_html_with_api
        return fetch_html_with_api(url, timeout=60) or b""

    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code < 400 and r.content:
            # Check if this is a SPA that needs JavaScript rendering
            # Look at first 5000 bytes to include <body> tag
            content_str = r.content[:5000].decode('utf-8', 'ignore')
            content_compact = re.sub(r'\s+', '', content_str.lower())

            # Detect Next.js, React, Vue, Angular apps that need JS rendering
            # This can be enhanced with more patterns as needed!!!!

            # Check if this is a known site that needs Playwright for category pages
            from urllib.parse import urlparse
            parsed_url = urlparse(url)

            # Site-specific rules for known SPA sites that need JS rendering
            # Add new sites here as you discover them
            # That means if html has only few news article content, and playwright is not triggered!!!!
            is_known_spa_site = (
                # The Star (Malaysia) - Vue.js app that loads articles via AJAX
                'thestar.com.my' in parsed_url.netloc
                # Add more sites here as needed, e.g.:
                # or 'example.com' in parsed_url.netloc
            )

            is_spa_shell = (
                # Empty containers (React/Vue/Angular apps)
                ('<divid="app"></div>' in content_compact or
                 '<divid="root"></div>' in content_compact or
                 '<divid="__next"></div>' in content_compact or
                 '<app-root' in content_str.lower()) or
                # Next.js with minimal content (has _next/static but few links)
                ('_next/static/' in content_str.lower() and
                 content_str.lower().count('<a ') < 10) or
                # Site-specific rules for known problematic sites
                is_known_spa_site
            )

            if is_spa_shell and use_playwright_fallback:
                if _VERBOSE: logger.info(f"[fetch_html] detected SPA shell (Next.js/React), using Playwright for {url}")
                try:
                    return fetch_html_with_playwright(url, timeout_ms=40000)
                except Exception as pw_e:
                    if _VERBOSE: logger.info(f"[fetch_html] Playwright fallback failed: {pw_e}")
                    # Return the original shell if Playwright fails
                    return r.content

            return r.content

        if _VERBOSE: logger.info(f"[fetch_html] requests status={r.status_code} for {url}")

        # If 403 or other error, try Playwright fallback (unless disabled)
        if r.status_code >= 400:
            if use_playwright_fallback:
                if _VERBOSE: logger.info(f"[fetch_html] trying Playwright fallback for {url}")
                try:
                    return fetch_html_with_playwright(url, timeout_ms=40000)
                except Exception as pw_e:
                    if _VERBOSE: logger.info(f"[fetch_html] Playwright fallback failed: {pw_e}")
            return b""

    except Exception as e:
        if _VERBOSE: logger.info(f"[fetch_html] requests failed {url}: {e}")

        # Try Playwright fallback on network errors too (unless disabled)
        if use_playwright_fallback:
            if _VERBOSE: logger.info(f"[fetch_html] trying Playwright fallback for {url}")
            try:
                return fetch_html_with_playwright(url, timeout_ms=40000)
            except Exception as pw_e:
                if _VERBOSE: logger.info(f"[fetch_html] Playwright fallback failed: {pw_e}")
        return b""


def fetch_title_fallback(session, url, use_brightdata=False) -> Optional[str]:
    """
    Fetch <title> tag when sitemap/RSS didn't provide one.
    Uses Playwright fallback for sites that block requests, or Bright Data if enabled.
    """
    try:
        data = fetch_html(session, url, timeout=(5, 8), use_brightdata=use_brightdata)

        if not data:
            if _VERBOSE: logger.info(f"[title] no data returned for {url}")
            return None

        # Decode bytes to string first - lxml.html.fromstring(bytes) ignores meta charset
        if isinstance(data, bytes):
            data = data.decode('utf-8', 'ignore')
        doc = html.fromstring(data)
        titles = doc.xpath("//title/text()")
        if titles:
            title = titles[0].strip()
            if _VERBOSE: logger.info(f"[title] extracted: {title}")
            return title
    except Exception as e:
        if _VERBOSE: logger.info(f"[title] fetch failed {url}: {e}")
    return None


def sniff_date(session, url, timeout=(3,5), use_brightdata=False):
    """
    Extract publication date from HTML (uses Playwright/Bright Data fallback if needed).
    Supports Indonesian relative dates like "4 jam yang lalu" (4 hours ago).
    """
    data = fetch_html(session, url, timeout=timeout, use_brightdata=use_brightdata)
    if not data:
        return None
    try:
        # Decode bytes first - lxml.html.fromstring(bytes) ignores meta charset
        if isinstance(data, bytes):
            data = data.decode('utf-8', 'ignore')
        doc = html.fromstring(data)
    except Exception:
        return None

    # Use the already-fetched HTML (don't re-fetch!)
    try:

        # JSON-LD
        for node in doc.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                data = json.loads(node.strip())
            except Exception:
                continue
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                for key in ("datePublished","dateCreated","uploadDate","dateModified"):
                    dt = parse_flexible(str(obj.get(key)))
                    if dt:
                        if _VERBOSE: logger.info(f"[sniff] JSON-LD hit {url}")
                        return dt

        # Meta/itemprop/time
        xps = [
            '//meta[@property="article:published_time"]/@content',
            '//meta[@name="pubdate"]/@content',
            '//meta[@name="date"]/@content',
            '//meta[@name="DC.date.issued"]/@content',
            '//*[@itemprop="datePublished"]/@content',
            '//*[@itemprop="datePublished"]/text()',
            '//time/@datetime',
            '//time/text()',
            # Common date class patterns (e.g., <p class="date">)
            '//*[@class="date"]/text()',
            '//*[@class="publish-date"]/text()',
            '//*[@class="post-date"]/text()',
            '//*[contains(@class, "date")]/text()',
        ]
        for xp in xps:
            try:
                vals = doc.xpath(xp)
                if vals:
                    dt = parse_flexible(str(vals[0]))
                    if dt:
                        if _VERBOSE: logger.info(f"[sniff] XPath hit {url}")
                        return dt
            except Exception:
                continue

        # Look for absolute dates in article-specific containers (brin.go.id pattern)
        # Check .announcement-time, .news-time, or similar classes first (more specific than sidebar)
        try:
            article_date_xps = [
                '//*[contains(@class, "announcement-time")]/text()',
                '//*[contains(@class, "news-time")]/text()',
                '//*[contains(@class, "article-time")]/text()',
                '//*[contains(@class, "publish-time")]/text()',
                '//article//*[contains(@class, "time")]/text()',
                '//article//*[contains(@class, "date")]/text()',
            ]
            for xp in article_date_xps:
                vals = doc.xpath(xp)
                if vals:
                    # Try to parse Indonesian/English dates
                    dt = parse_flexible(vals[0].strip())
                    if dt:
                        if _VERBOSE: logger.info(f"[sniff] Article-specific date container hit {url}")
                        return dt
        except Exception:
            pass

        try:
            texts = doc.xpath(
                '//*[re:test(normalize-space(.), "published|updated", "i")]//text()',
                namespaces={"re": "http://exslt.org/regular-expressions"},
            )
            joined = " ".join(t.strip() for t in texts if t and len(t.strip()) < 120)
            dt = parse_flexible(joined)
            if dt:
                if _VERBOSE: logger.info(f"[sniff] label hit {url}")
                return dt
        except etree.XPathEvalError:
            # EXSLT not available? fall back to Python-side regex
            pass

        # Fallback: search for date patterns in the first part of the article
        # (useful for sites like pna.gov.ph that don't use semantic date tags)
        try:
            # Get text from article body, byline, or header areas
            body_texts = doc.xpath('//article//text() | //div[contains(@class, "article")]//text() | //div[contains(@class, "byline")]//text() | //div[contains(@class, "meta")]//text()')[:50]
            # If no semantic HTML found, try broader search (for old PHP sites like vientianetimes.org.la)
            if not body_texts:
                body_texts = doc.xpath('//body//text()')[:100]
            combined_text = " ".join(t.strip() for t in body_texts if t and len(t.strip()) > 2)[:2000]

            # Try to find common date patterns
            date_patterns = [
                # "October 27, 2025", "Jan 5, 2024"
                r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}',
                # "27 October 2025"
                r'\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}',
                # Thai dates: "28 มกราคม 2568"
                r'\d{1,2}\s+(?:มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s+\d{4}',
            ]

            for pattern in date_patterns:
                matches = re.findall(pattern, combined_text, re.IGNORECASE)
                if matches:
                    # Try the first match
                    dt = parse_flexible(matches[0])
                    if dt:
                        if _VERBOSE: logger.info(f"[sniff] pattern hit {url}")
                        return dt
        except Exception:
            pass

        # LAST RESORT: Indonesian relative dates (e.g., "4 jam yang lalu")
        # Only use this if no absolute date was found (since relative dates might be from sidebar)
        try:
            from datetime import timedelta
            from .crawler import now_berlin

            # Get text from main article area only, not sidebar
            # Try to be specific to avoid sidebar dates
            date_texts = doc.xpath(
                '//article//*[contains(text(), "yang lalu") or contains(text(), "ago")]//text() | '
                '//*[contains(@class, "detail")]//*[contains(text(), "yang lalu")]//text() | '
                '//*[contains(@class, "news-time")]//*[contains(text(), "yang lalu")]//text()'
            )
            # If no article-specific dates, fall back to any "yang lalu" text
            if not date_texts:
                date_texts = doc.xpath('//*[contains(text(), "yang lalu") or contains(text(), "ago")]//text()')

            combined = " ".join(t.strip() for t in date_texts if t and len(t.strip()) < 200)

            if combined:
                txt = combined.lower()
                if "jam" in txt or "hour" in txt:
                    m = re.search(r'(\d+)', txt)
                    if m:
                        dt = now_berlin() - timedelta(hours=int(m.group(1)))
                        if _VERBOSE: logger.info(f"[sniff] Indonesian relative (hours) hit {url}")
                        return dt
                elif "menit" in txt or "minute" in txt:
                    m = re.search(r'(\d+)', txt)
                    if m:
                        dt = now_berlin() - timedelta(minutes=int(m.group(1)))
                        if _VERBOSE: logger.info(f"[sniff] Indonesian relative (minutes) hit {url}")
                        return dt
                elif "hari" in txt or "day" in txt:
                    m = re.search(r'(\d+)', txt)
                    if m:
                        dt = now_berlin() - timedelta(days=int(m.group(1)))
                        if _VERBOSE: logger.info(f"[sniff] Indonesian relative (days) hit {url}")
                        return dt
        except Exception as e:
            if _VERBOSE: logger.info(f"[sniff] Indonesian relative date extraction failed: {e}")
            pass

    except Exception:
        return None

def _fetch_sitemap_dates(session, site_url):
    """
    Fetch sitemap and build a URL -> date lookup dictionary.
    Returns dict of {url: datetime} for articles found in sitemap.
    """
    from urllib.parse import urljoin
    import xml.etree.ElementTree as ET

    sitemap_dates = {}

    try:
        # Discover sitemaps from robots.txt
        robots_url = urljoin(site_url.rstrip('/') + '/', 'robots.txt')
        sitemap_urls = []

        try:
            r = session.get(robots_url, timeout=(3, 5))
            if r.status_code == 200:
                for line in r.text.splitlines():
                    if line.lower().strip().startswith('sitemap:'):
                        sitemap_url = line.split(':', 1)[1].strip()
                        sitemap_urls.append(sitemap_url)
        except Exception:
            pass

        # Fallback: try common sitemap paths
        if not sitemap_urls:
            sitemap_urls = [
                urljoin(site_url.rstrip('/') + '/', 'sitemap.xml'),
                urljoin(site_url.rstrip('/') + '/', 'sitemap-news.xml'),
            ]

        # Fetch sitemaps (limit to first 2 to avoid excessive requests)
        for sitemap_url in sitemap_urls[:2]:
            try:
                r = session.get(sitemap_url, timeout=(5, 10))
                if r.status_code != 200:
                    continue

                # Parse XML
                root = ET.fromstring(r.content)

                # Handle both regular sitemaps and news sitemaps
                namespaces = {
                    'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9',
                    'news': 'http://www.google.com/schemas/sitemap-news/0.9'
                }

                # Try news sitemap format first
                for url_elem in root.findall('.//sm:url', namespaces):
                    loc_elem = url_elem.find('sm:loc', namespaces)
                    if loc_elem is None or not loc_elem.text:
                        continue

                    url = loc_elem.text.strip()

                    # Try news:publication_date first
                    date_elem = url_elem.find('.//news:publication_date', namespaces)
                    if date_elem is None or not date_elem.text:
                        # Fall back to lastmod
                        date_elem = url_elem.find('sm:lastmod', namespaces)

                    if date_elem is not None and date_elem.text:
                        dt = parse_flexible(date_elem.text.strip())
                        if dt:
                            sitemap_dates[url] = dt

                if _VERBOSE and sitemap_dates: logger.info(f"[sniff] Loaded {len(sitemap_dates)} dates from sitemap: {sitemap_url}")

                # If we found dates, no need to check more sitemaps
                if sitemap_dates:
                    break

            except Exception as e:
                if _VERBOSE: logger.info(f"[sniff] Failed to fetch sitemap {sitemap_url}: {e}")
                continue

    except Exception as e:
        if _VERBOSE: logger.info(f"[sniff] Sitemap date lookup failed: {e}")

    return sitemap_dates

def enrich_dates_light(session, undated_hints, cap=100, workers=12, site_url=None, use_brightdata=False):
    """
    Enrich undated hints by extracting dates from URLs, sitemaps, or fetching pages.
    Priority order:
    1. Parse dates from URL patterns (fast - /2025/10/29/)
    2. Check sitemap for article dates (medium - reliable)
    3. Scrape HTML pages (slow - last resort)
    """
    from datetime import datetime

    take = undated_hints[:cap]
    out = []
    got = 0

    # First pass: Try to extract dates from URL patterns (instant, no fetching)
    # Pattern 1: /YYYY/MM/DD/ format
    url_date_pattern_slashes = re.compile(r'/(\d{4})/(\d{1,2})/(\d{1,2})/')
    # Pattern 2: YYYY-MM-DD anywhere in URL (for Reuters-style: article-name-2025-10-27/)
    url_date_pattern_dashes = re.compile(r'[-/](\d{4})-(\d{1,2})-(\d{1,2})/?')

    for h in take:
        if h.published_at:  # Already has date
            continue

        # Try slash pattern first
        match = url_date_pattern_slashes.search(h.url)
        if not match:
            # Try dash pattern
            match = url_date_pattern_dashes.search(h.url)

        if match:
            try:
                year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                from .crawler import BERLIN_TZ
                # Set to noon (12:00) instead of midnight to avoid range issues with partial-day windows
                h.published_at = datetime(year, month, day, 12, 0, 0, tzinfo=BERLIN_TZ)
                got += 1
                if _VERBOSE: logger.info(f"[sniff] URL date: {h.url} -> {year}-{month:02d}-{day:02d}")
            except (ValueError, Exception) as e:
                if _VERBOSE: logger.info(f"[sniff] Invalid date in URL {h.url}: {e}")

    # Second pass: Check sitemap for articles (medium speed, high reliability)
    needs_sitemap = [h for h in take if not h.published_at]
    if needs_sitemap and site_url:
        sitemap_dates = _fetch_sitemap_dates(session, site_url)
        if sitemap_dates:
            for h in needs_sitemap:
                # Normalize URL for matching (remove utm params, etc.)
                norm_h_url = h.url.split('?')[0].split('#')[0].lower()

                # Try exact match and common variations
                for sitemap_url, sitemap_date in sitemap_dates.items():
                    norm_sitemap_url = sitemap_url.split('?')[0].split('#')[0].lower()
                    if norm_h_url == norm_sitemap_url:
                        h.published_at = sitemap_date
                        got += 1
                        if _VERBOSE: logger.info(f"[sniff] Sitemap date: {h.url} -> {sitemap_date.date()}")
                        break

    # Third pass: For URLs still without dates, fetch HTML (slower)
    needs_fetch = [h for h in take if not h.published_at]
    if needs_fetch:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(sniff_date, session, h.url, (3,5), use_brightdata): h for h in needs_fetch}
            for fut in as_completed(futs):
                h = futs[fut]
                dt = fut.result()
                if dt:
                    h.published_at = dt
                    got += 1

    out = take  # All hints (with or without dates)

    if _VERBOSE: logger.info(f"[sniff] dates recovered: {got}/{len(take)}")
    return out
