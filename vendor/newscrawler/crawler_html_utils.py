# utilities.py
import re
from datetime import datetime, timezone
from .crawler_playwright import fetch_html_with_playwright
import requests
from src.logger import get_logger

logger = get_logger(__name__)

from src.config import CRAWLER_VERBOSE as _VERBOSE


# A publisher answering 429 or 503 is asking us to stop. Neither a browser nor the
# next guessed URL is an acceptable answer to that.
THROTTLE_STATUSES = (429, 503)


class HostThrottled(requests.exceptions.RequestException):
    """Raised instead of sending a request to a host that has throttled this pass."""

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
            if use_playwright_fallback and r.status_code not in THROTTLE_STATUSES:
                if _VERBOSE: logger.info(f"[fetch_html] trying Playwright fallback for {url}")
                try:
                    return fetch_html_with_playwright(url, timeout_ms=40000)
                except Exception as pw_e:
                    if _VERBOSE: logger.info(f"[fetch_html] Playwright fallback failed: {pw_e}")
            return b""

    except HostThrottled:
        raise
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
