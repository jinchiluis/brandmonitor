#!/usr/bin/env python3
"""
Google News Feed Crawler
------------------------
Dedicated module for crawling Google News RSS feeds with proper redirect unwrapping.

Features:
- EU consent banner handling with Playwright (headful one-time setup)
- Cookie persistence for headless subsequent runs
- Proper HTTP redirect following (not just query param parsing)
- Returns clean ArticleHints compatible with main crawler

Usage:
    # First-time setup (EU users):
    python crawler_google_feeds.py --setup-consent

    # Test feed crawling:
    python crawler_google_feeds.py --test-site example.com

    # From code:
    from crawler_google_feeds import GoogleNewsFeedCrawler
    crawler = GoogleNewsFeedCrawler()
    hints = crawler.fetch_feed_for_site("example.com")
"""

from __future__ import annotations
import json
import argparse
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit, parse_qs, urljoin
from datetime import datetime

import requests
import feedparser
import tldextract
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from src.logger import get_logger

logger = get_logger(__name__)

# Import from existing crawler modules
try:
    from .crawler import ArticleHint, parse_dt, normalize_url, BERLIN_TZ
except ImportError:
    # Fallback for standalone testing
    from dataclasses import dataclass
    from dateutil import parser as dateparser
    from datetime import timezone

    BERLIN_TZ = timezone.utc

    @dataclass
    class ArticleHint:
        url: str
        published_at: Optional[datetime]
        title: Optional[str]
        source: str

    def parse_dt(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        try:
            dt = dateparser.parse(val)
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(BERLIN_TZ)
        except Exception:
            return None

    def normalize_url(url: str) -> str:
        from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
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


class GoogleNewsFeedCrawler:
    """
    Handles Google News RSS feeds with proper EU consent and redirect unwrapping.
    """

    DEFAULT_COOKIE_FILE = Path(__file__).parent / "playwright_cookies" / "google_news_cookies.json"
    GOOGLE_NEWS_BASE = "https://news.google.com"

    def __init__(self, cookie_file: Optional[str] = None, verbose: bool = False):
        """
        Args:
            cookie_file: Path to JSON file storing Google News cookies
            verbose: Enable debug output
        """
        self.cookie_file = Path(cookie_file or self.DEFAULT_COOKIE_FILE)
        self.verbose = verbose
        # Ensure playwright_cookies directory exists
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._load_cookies()

    def _load_cookies(self) -> None:
        """Load cookies from file into session."""
        if not self.cookie_file.exists():
            if self.verbose:
                logger.info(f"[google] No cookie file found at {self.cookie_file}")
            return

        try:
            with open(self.cookie_file, 'r') as f:
                cookies = json.load(f)

            for cookie in cookies:
                self.session.cookies.set(
                    cookie['name'],
                    cookie['value'],
                    domain=cookie.get('domain'),
                    path=cookie.get('path', '/')
                )

            if self.verbose:
                logger.info(f"[google] Loaded {len(cookies)} cookies from {self.cookie_file}")

        except Exception as e:
            if self.verbose:
                logger.info(f"[google] Failed to load cookies: {e}")

    def _save_cookies(self, cookies: List[dict]) -> None:
        """Save cookies to file."""
        try:
            with open(self.cookie_file, 'w') as f:
                json.dump(cookies, f, indent=2)

            if self.verbose:
                logger.info(f"[google] Saved {len(cookies)} cookies to {self.cookie_file}")

        except Exception as e:
            if self.verbose:
                logger.info(f"[google] Failed to save cookies: {e}")

    def setup_consent_headful(self, timeout_seconds: int = 120) -> bool:
        """
        Open Google News in headful browser for user to click consent.
        Saves cookies for future headless use.

        Args:
            timeout_seconds: How long to wait for user to complete consent

        Returns:
            True if cookies were saved successfully
        """
        logger.info("\n" + "="*60)
        logger.info("Google News EU Consent Setup")
        logger.info("="*60)
        logger.info("\nA browser window will open. Please:")
        logger.info("1. Click 'Accept all' or configure your consent preferences")
        logger.info("2. Wait for the page to fully load")
        logger.info("3. The browser will close automatically\n")
        logger.info(f"Timeout: {timeout_seconds} seconds")
        logger.info("="*60 + "\n")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    viewport={'width': 1280, 'height': 720},
                    user_agent=self.session.headers['User-Agent']
                )
                page = context.new_page()

                # Navigate to Google News
                logger.info(f"[google] Opening {self.GOOGLE_NEWS_BASE}...")
                page.goto(self.GOOGLE_NEWS_BASE, wait_until="networkidle", timeout=30000)

                # Wait for user to handle consent
                logger.info("[google] Waiting for you to accept consent...")
                logger.info("[google] (The page should show news articles when ready)")

                start_time = time.time()
                consent_handled = False

                # Check if consent was handled by looking for typical post-consent elements
                while time.time() - start_time < timeout_seconds:
                    try:
                        # Check if we can see news articles (sign that consent was handled)
                        articles = page.locator('article').count()
                        if articles > 3:
                            consent_handled = True
                            logger.info(f"[google] Detected {articles} articles - consent appears handled!")
                            break
                    except Exception:
                        pass

                    time.sleep(2)

                if not consent_handled:
                    logger.info("[google] WARNING: Could not confirm consent was handled")
                    logger.info("[google] Saving cookies anyway - they may not be valid")

                # Extract and save cookies
                cookies = context.cookies()
                playwright_cookies = [
                    {
                        'name': c['name'],
                        'value': c['value'],
                        'domain': c['domain'],
                        'path': c['path'],
                        'expires': c.get('expires', -1),
                        'httpOnly': c.get('httpOnly', False),
                        'secure': c.get('secure', False),
                    }
                    for c in cookies
                ]

                self._save_cookies(playwright_cookies)

                # Give user a moment to see the result
                time.sleep(2)

                browser.close()

                logger.info("\n" + "="*60)
                logger.info(f"Setup complete! Cookies saved to {self.cookie_file}")
                logger.info("You can now run headless crawls.")
                logger.info("="*60 + "\n")

                return True

        except Exception as e:
            logger.info(f"\n[google] ERROR during consent setup: {e}")
            return False

    def unwrap_redirect(self, google_news_url: str, timeout: int = 10) -> Optional[str]:
        """
        Decode Google News redirect URL to get the actual article URL.

        Google News uses various redirect URL formats:
        - https://news.google.com/rss/articles/CBMi...?oc=5  (base64 encoded)
        - https://news.google.com/rss/articles/...?url=ENCODED_URL  (query param)
        - https://news.google.com/articles/...  (needs decoding)

        Args:
            google_news_url: The redirect URL from Google News
            timeout: Request timeout in seconds (unused for now, kept for compatibility)

        Returns:
            The actual article URL, or None if decoding failed
        """
        # First try: extract from query param
        try:
            parsed = urlsplit(google_news_url)
            if "news.google.com" in parsed.netloc:
                qs = parse_qs(parsed.query)
                # Try 'url' param first, then 'u'
                for param in ('url', 'u'):
                    if param in qs and qs[param]:
                        real_url = qs[param][0].strip()
                        if real_url and real_url.startswith('http'):
                            if self.verbose:
                                logger.info(f"[google] Unwrapped from query param: {real_url}")
                            return real_url
        except Exception as e:
            if self.verbose:
                logger.info(f"[google] Query param extraction failed: {e}")

        # Second try: use googlenewsdecoder library to decode base64 encoded URLs
        try:
            if "news.google.com" not in google_news_url:
                return google_news_url  # Already unwrapped

            # Try importing googlenewsdecoder
            try:
                from googlenewsdecoder import new_decoderv1
            except ImportError:
                if self.verbose:
                    logger.info("[google] googlenewsdecoder not installed, install with: pip install googlenewsdecoder")
                return None

            # Decode the URL (returns dict with 'decoded_url' key)
            result = new_decoderv1(google_news_url)

            # Extract the actual URL from the result
            if isinstance(result, dict):
                decoded_url = result.get('decoded_url')
            elif isinstance(result, str):
                decoded_url = result
            else:
                decoded_url = None

            if decoded_url and decoded_url != google_news_url and decoded_url.startswith('http'):
                if self.verbose:
                    logger.info(f"[google] Decoded URL: {decoded_url}")
                return decoded_url

            if self.verbose:
                logger.info(f"[google] Decoding returned same URL or invalid result: {result}")

        except Exception as e:
            if self.verbose:
                logger.info(f"[google] URL decoding failed: {e}")

        # Failed to unwrap - return None
        if self.verbose:
            logger.info(f"[google] Could not unwrap: {google_news_url}")
        return None

    def build_feed_url(self, site_domain: str, language: str = "en-US", country: str = "US") -> str:
        """
        Build Google News RSS feed URL for a site.

        Args:
            site_domain: Domain to search (e.g., "example.com")
            language: Language code (e.g., "en-US", "en-PH")
            country: Country code (e.g., "US", "PH")

        Returns:
            Google News RSS feed URL
        """
        # Normalize domain (remove www. if present)
        if site_domain.startswith("www."):
            site_domain = site_domain[4:]

        return (
            f"{self.GOOGLE_NEWS_BASE}/rss/search"
            f"?q=site:{site_domain}&hl={language}&gl={country}&ceid={country}:en"
        )

    def fetch_feed_for_site(
        self,
        site_domain: str,
        language: str = "en-US",
        country: str = "US",
        max_items: int = 100
    ) -> List[ArticleHint]:
        """
        Fetch and parse Google News RSS feed for a site.

        Args:
            site_domain: Domain to search (e.g., "example.com")
            language: Language code
            country: Country code
            max_items: Maximum number of items to return

        Returns:
            List of ArticleHint objects with unwrapped URLs
        """
        feed_url = self.build_feed_url(site_domain, language, country)

        if self.verbose:
            logger.info(f"[google] Fetching feed: {feed_url}")

        try:
            # Fetch the feed
            resp = self.session.get(feed_url, timeout=15)
            resp.raise_for_status()

            if self.verbose:
                logger.info(f"[google] Feed fetched: {len(resp.content)} bytes")

            # Parse with feedparser
            parsed = feedparser.parse(resp.content)
            entries = getattr(parsed, "entries", []) or []

            if self.verbose:
                logger.info(f"[google] Found {len(entries)} entries")

            hints = []

            # Extract domain for filtering - include subdomain if specified
            ext = tldextract.extract(site_domain)
            target_domain = f"{ext.domain}.{ext.suffix}" if ext.suffix else site_domain
            # If original site_domain had a subdomain, we need to match it exactly
            target_subdomain = ext.subdomain  # e.g., "th" for th.china-embassy.gov.cn

            for idx, entry in enumerate(entries[:max_items]):
                try:
                    # Get the Google News URL
                    google_url = (entry.get("link") or entry.get("id") or "").strip()
                    if not google_url:
                        continue

                    # Unwrap to get real article URL
                    real_url = self.unwrap_redirect(google_url)
                    if not real_url:
                        if self.verbose:
                            logger.info(f"[google] Skipping entry {idx+1}: could not unwrap")
                        continue

                    # Verify it's from the target domain
                    url_ext = tldextract.extract(real_url)
                    url_domain = f"{url_ext.domain}.{url_ext.suffix}" if url_ext.suffix else ""
                    url_subdomain = url_ext.subdomain

                    # Check base domain first
                    if url_domain != target_domain:
                        if self.verbose:
                            logger.info(f"[google] Skipping {real_url}: domain mismatch ({url_domain} != {target_domain})")
                        continue

                    # If target had a specific subdomain (e.g., "th"), enforce exact subdomain match
                    if target_subdomain and url_subdomain != target_subdomain:
                        if self.verbose:
                            logger.info(f"[google] Skipping {real_url}: subdomain mismatch ({url_subdomain} != {target_subdomain})")
                        continue

                    # Extract metadata
                    title = entry.get("title") or None

                    # Parse date
                    dt_raw = entry.get("published") or entry.get("updated") or entry.get("created")
                    published = None
                    if dt_raw:
                        published = parse_dt(dt_raw)

                    # Fallback to struct_time if string parsing failed
                    if not published:
                        for key in ("published_parsed", "updated_parsed", "created_parsed"):
                            t = entry.get(key)
                            if t and hasattr(t, "tm_year"):
                                try:
                                    import time as _time
                                    ts = _time.mktime(t)
                                    from datetime import datetime as _datetime
                                    published = _datetime.fromtimestamp(ts, tz=BERLIN_TZ)
                                    break
                                except Exception:
                                    continue

                    hints.append(ArticleHint(
                        url=normalize_url(real_url),
                        published_at=published,
                        title=title,
                        source="rss-google"
                    ))

                    if self.verbose:
                        logger.info(f"[google] Entry {idx+1}: {title[:60] if title else 'No title'}")

                except Exception as e:
                    if self.verbose:
                        logger.info(f"[google] Error processing entry {idx+1}: {e}")
                    continue

            if self.verbose:
                logger.info(f"[google] Extracted {len(hints)} valid hints")

            return hints

        except Exception as e:
            if self.verbose:
                logger.info(f"[google] Feed fetch failed: {e}")
            return []


def main():
    """CLI for Google News feed crawler."""
    parser = argparse.ArgumentParser(
        description="Google News RSS Feed Crawler with EU consent handling"
    )
    parser.add_argument(
        "--setup-consent",
        action="store_true",
        help="Run headful browser to accept EU consent (one-time setup)"
    )
    parser.add_argument(
        "--test-site",
        type=str,
        help="Test feed crawling for a specific site domain (e.g., 'example.com')"
    )
    parser.add_argument(
        "--cookie-file",
        type=str,
        default=GoogleNewsFeedCrawler.DEFAULT_COOKIE_FILE,
        help="Path to cookie storage file"
    )
    parser.add_argument(
        "--language",
        type=str,
        default="en-US",
        help="Language code (default: en-US)"
    )
    parser.add_argument(
        "--country",
        type=str,
        default="US",
        help="Country code (default: US)"
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=50,
        help="Maximum items to fetch (default: 50)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output"
    )

    args = parser.parse_args()

    crawler = GoogleNewsFeedCrawler(
        cookie_file=args.cookie_file,
        verbose=args.verbose
    )

    if args.setup_consent:
        success = crawler.setup_consent_headful()
        if success:
            logger.info("\nConsent setup completed successfully!")
            logger.info("You can now use --test-site to verify it works.")
        else:
            logger.info("\nConsent setup failed. Please try again.")
        return

    if args.test_site:
        logger.info(f"\nTesting Google News feed for: {args.test_site}")
        logger.info("="*60)

        hints = crawler.fetch_feed_for_site(
            args.test_site,
            language=args.language,
            country=args.country,
            max_items=args.max_items
        )

        logger.info(f"\n{'='*60}")
        logger.info(f"Results: {len(hints)} articles found")
        logger.info("="*60 + "\n")

        for idx, hint in enumerate(hints, 1):
            logger.info(f"{idx}. {hint.title or 'No title'}")
            logger.info(f"   URL: {hint.url}")
            logger.info(f"   Published: {hint.published_at.isoformat() if hint.published_at else 'Unknown'}")
            logger.info("")

        return

    # No action specified
    parser.print_help()
    logger.info("\n" + "="*60)
    logger.info("Quick start:")
    logger.info("  1. Setup consent:  python crawler_google_feeds.py --setup-consent")
    logger.info("  2. Test crawling:  python crawler_google_feeds.py --test-site example.com")
    logger.info("="*60)


if __name__ == "__main__":
    main()
