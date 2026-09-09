#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bright Data crawler with Unlocker API and Facebook Scraper
-----------------------------------------------------------
Provides two main functionalities:

1. Unlocker API (cheap, fast) - For fetching HTML from any website
2. Facebook Scraper API - For scraping Facebook posts with structured data

Usage:
    from crawler_brightdata import fetch_html_with_api, fetch_facebook_posts

    # Fetch HTML from any website
    html = fetch_html_with_api("https://phnompenhpost.com")

    # Fetch Facebook posts
    result = fetch_facebook_posts(
        facebook_urls=["https://www.facebook.com/LeBron/"],
        num_of_posts=50,
        start_date="01-01-2025",
        end_date="02-28-2025"
    )
"""

import os
import sys
import io
import json
import requests
from typing import Optional
from dotenv import load_dotenv

from src.logger import get_logger
logger = get_logger(__name__)

# Fix Windows console encoding
# if sys.platform == 'win32':
#     sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Load environment variables
load_dotenv()

# Simple in-memory cache to avoid re-fetching same URLs
_HTML_CACHE = {}

# Per-site call counter to limit Bright Data API usage
_SITE_CALL_COUNT = {}
_MAX_CALLS_PER_SITE = 70

def clear_html_cache():
    """Clear the HTML cache and call counters (call between different sites/runs)."""
    global _HTML_CACHE, _SITE_CALL_COUNT
    _HTML_CACHE = {}
    _SITE_CALL_COUNT = {}
    if len(_HTML_CACHE) > 0:
        logger.info(f"[brightdata] Cleared HTML cache ({len(_HTML_CACHE)} entries)")

def reset_site_counter(site_domain: str):
    """Reset the call counter for a specific site."""
    global _SITE_CALL_COUNT
    if site_domain in _SITE_CALL_COUNT:
        del _SITE_CALL_COUNT[site_domain]

def _poll_snapshot(api_key: str, snapshot_id: str, poll_interval: int = 5, max_wait_time: int = 300, timeout: int = 60) -> Optional[list]:
    """
    Poll the Bright Data snapshot endpoint until data is ready.

    Args:
        api_key: Bright Data API key
        snapshot_id: Snapshot ID from initial request
        poll_interval: Seconds between polls (default: 5)
        max_wait_time: Max seconds to wait (default: 300 = 5 min)
        timeout: HTTP request timeout

    Returns:
        List of parsed JSON objects or None if failed/timeout
    """
    import time

    download_url = f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}"

    headers = {
        'Authorization': f'Bearer {api_key}'
    }

    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_wait_time:
            logger.info(f"[brightdata-scraper] Timeout after {elapsed:.1f}s - data not ready")
            logger.info(f"[brightdata-scraper] You can manually check: {download_url}")
            return None

        try:
            # Try to download - if not ready, API will return error
            logger.info(f"[brightdata-scraper] Checking snapshot... (elapsed: {elapsed:.1f}s)")
            download_response = requests.get(download_url, headers=headers, timeout=timeout)

            if download_response.status_code == 200:
                # Data is ready! Parse JSONL response
                logger.info(f"[brightdata-scraper] Data ready! Downloading...")
                results = []
                for line in download_response.text.strip().splitlines():
                    if line.strip():
                        try:
                            obj = json.loads(line)
                            results.append(obj)
                        except json.JSONDecodeError as e:
                            logger.info(f"[brightdata-scraper] Warning: Could not parse line: {e}")

                logger.info(f"[brightdata-scraper] Downloaded {len(results)} JSON objects")
                return results

            elif download_response.status_code == 404:
                # Not ready yet, wait and retry
                logger.info(f"[brightdata-scraper] Not ready yet, waiting {poll_interval}s...")
                time.sleep(poll_interval)

            else:
                # Some other error
                logger.info(f"[brightdata-scraper] Download error: {download_response.status_code}")
                logger.info(f"[brightdata-scraper] Response: {download_response.text[:500]}")
                return None

        except Exception as e:
            logger.info(f"[brightdata-scraper] Polling error: {e}")
            time.sleep(poll_interval)

def fetch_facebook_posts(
    facebook_urls: list,
    num_of_posts: int = 10,
    start_date: str = "",
    end_date: str = "",
    posts_to_exclude: list = None,
    timeout: int = 120,
    poll_interval: int = 5,
    max_wait_time: int = 300
) -> Optional[list]:
    """
    Fetch Facebook posts using Bright Data Scraper API.

    Handles both synchronous (immediate) and asynchronous (snapshot-based) responses.

    Args:
        facebook_urls: List of Facebook page URLs (e.g., ["https://www.facebook.com/LeBron/"])
        num_of_posts: Number of posts to fetch per URL (default: 10)
        start_date: Start date in format "MM-DD-YYYY" (e.g., "01-01-2025", default: "")
        end_date: End date in format "MM-DD-YYYY" (e.g., "02-28-2025", default: "")
        posts_to_exclude: List of post IDs to exclude (default: None)
        timeout: Request timeout in seconds for each HTTP request
        poll_interval: How often to check if snapshot is ready (seconds, default: 5)
        max_wait_time: Maximum time to wait for snapshot (seconds, default: 300 = 5 min)

    Returns:
        List of JSON objects from Bright Data API (JSONL format parsed), or None if failed
        Each object in the list represents one scraped post/page data.

    Requires BRIGHTDATA_API_KEY in environment.

    Example:
        result = fetch_facebook_posts(
            facebook_urls=["https://www.facebook.com/LeBron/"],
            num_of_posts=50,
            start_date="01-01-2025",
            end_date="02-28-2025"
        )
    """
    api_key = os.getenv('BRIGHTDATA_API_KEY')
    dataset_id = 'gd_lkaxegm826bjpoo9m5'

    if not api_key:
        logger.info("[brightdata-scraper] ERROR: Missing BRIGHTDATA_API_KEY")
        return None

    # Build input array for each URL
    input_data = []
    for url in facebook_urls:
        entry = {
            "url": url,
            "num_of_posts": num_of_posts,
            "start_date": start_date,
            "end_date": end_date
        }

        if posts_to_exclude:
            entry["posts_to_not_include"] = posts_to_exclude
        else:
            entry["posts_to_not_include"] = []

        input_data.append(entry)

    try:
        logger.info(f"[brightdata-scraper] Fetching {len(facebook_urls)} Facebook page(s), {num_of_posts} posts each")

        endpoint = f"https://api.brightdata.com/datasets/v3/scrape"

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        params = {
            'dataset_id': dataset_id,
            'notify': 'false',
            'include_errors': 'true'
        }

        payload = {
            "input": input_data
        }

        logger.info(f"[brightdata-scraper] Sending request to dataset {dataset_id}...")
        response = requests.post(
            endpoint,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout
        )

        # Handle both 200 (sync/complete) and 202 (async/accepted) responses
        if response.status_code in [200, 202]:
            response_text = response.text
            logger.info(f"[brightdata-scraper] Got response ({len(response_text)} chars)")

            # Check if this is an async response (contains snapshot_id)
            try:
                first_line = response_text.strip().splitlines()[0] if response_text.strip() else ""
                if first_line:
                    first_obj = json.loads(first_line)

                    if "snapshot_id" in first_obj:
                        # Async response - poll for results
                        snapshot_id = first_obj["snapshot_id"]
                        logger.info(f"[brightdata-scraper] Async response (HTTP {response.status_code}). Snapshot ID: {snapshot_id}")
                        logger.info(f"[brightdata-scraper] Polling every {poll_interval}s (max {max_wait_time}s)")

                        return _poll_snapshot(api_key, snapshot_id, poll_interval, max_wait_time, timeout)
            except (json.JSONDecodeError, IndexError, KeyError):
                # Not async, continue with normal parsing
                pass

            # Synchronous response - parse JSONL immediately
            logger.info(f"[brightdata-scraper] Parsing synchronous response...")
            results = []
            for line in response_text.strip().splitlines():
                if line.strip():  # Skip empty lines
                    try:
                        obj = json.loads(line)
                        results.append(obj)
                    except json.JSONDecodeError as e:
                        logger.info(f"[brightdata-scraper] Warning: Could not parse line: {e}")
                        logger.info(f"[brightdata-scraper] Line content: {line[:200]}")

            logger.info(f"[brightdata-scraper] Parsed {len(results)} JSON objects")
            return results
        else:
            logger.info(f"[brightdata-scraper] Error: {response.status_code}")
            logger.info(f"[brightdata-scraper] Response: {response.text[:500]}")
            return None

    except Exception as e:
        logger.info(f"[brightdata-scraper] Exception: {e}")
        import traceback
        traceback.print_exc()
        return None

def fetch_html_with_api(
    url: str,
    timeout: int = 60
) -> Optional[bytes]:
    """
    Fetch HTML using Bright Data Unlocker API (HTTP-based, cheaper).

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds

    Returns:
        HTML content as bytes, or None if failed

    Requires BRIGHTDATA_UNLOCKER_API_KEY and BRIGHTDATA_UNLOCKER_ZONE in environment.
    """
    global _HTML_CACHE, _SITE_CALL_COUNT, _MAX_CALLS_PER_SITE

    # Check cache first
    if url in _HTML_CACHE:
        logger.info(f"[brightdata-api] Using cached HTML for {url}")
        return _HTML_CACHE[url]

    # Extract domain from URL for per-site limiting
    from urllib.parse import urlparse
    site_domain = urlparse(url).netloc

    # Check if we've exceeded the limit for this site
    call_count = _SITE_CALL_COUNT.get(site_domain, 0)
    if call_count >= _MAX_CALLS_PER_SITE:
        logger.info(f"[brightdata-api] LIMIT REACHED: {call_count}/{_MAX_CALLS_PER_SITE} calls for {site_domain} - skipping {url}")
        return None

    api_key = os.getenv('BRIGHTDATA_UNLOCKER_API_KEY')
    zone = os.getenv('BRIGHTDATA_UNLOCKER_ZONE', 'web_unlocker1')

    if not api_key:
        logger.info("[brightdata-api] ERROR: Missing BRIGHTDATA_UNLOCKER_API_KEY")
        return None

    # Increment counter BEFORE making the call
    _SITE_CALL_COUNT[site_domain] = call_count + 1

    try:
        logger.info(f"[brightdata-api] Fetching {url} via Unlocker API (zone: {zone}) [{_SITE_CALL_COUNT[site_domain]}/{_MAX_CALLS_PER_SITE}]")

        endpoint = 'https://api.brightdata.com/request'

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            "zone": zone,
            "url": url,
            "format": "raw"
        }

        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=timeout
        )

        if response.status_code == 200:
            html_content = response.text
            if html_content:
                html_bytes = html_content.encode('utf-8', 'ignore')
                # Cache the result
                _HTML_CACHE[url] = html_bytes

                # # DEBUG: Save HTML to debug folder
                # try:
                #     import hashlib
                #     debug_dir = "brightdata_debug"
                #     os.makedirs(debug_dir, exist_ok=True)

                #     # Create filename from URL hash + domain
                #     url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                #     from urllib.parse import urlparse as parse_url
                #     domain = parse_url(url).netloc.replace('.', '_')
                #     path_part = parse_url(url).path.replace('/', '_')[:50]
                #     filename = f"{domain}{path_part}_{url_hash}.html"

                #     filepath = os.path.join(debug_dir, filename)
                #     with open(filepath, 'wb') as f:
                #         f.write(html_bytes)
                #     print(f"[brightdata-api] Saved debug HTML to: {filepath}")
                # except Exception as e:
                #     print(f"[brightdata-api] Warning: Could not save debug HTML: {e}")

                logger.info(f"[brightdata-api] Got {len(html_content)} characters of HTML (and put to cache)")
                return html_bytes
            else:
                logger.info(f"[brightdata-api] No HTML content in response")
                return None
        else:
            logger.info(f"[brightdata-api] Error: {response.status_code}")
            logger.info(f"[brightdata-api] Response: {response.text[:500]}")
            return None

    except Exception as e:
        logger.info(f"[brightdata-api] Exception: {e}")
        return None

# def fetch_html_with_browser(
#     url: str,
#     timeout_ms: int = 60000
# ) -> Optional[bytes]:
#     """
#     Fetch HTML using Bright Data Scraping Browser (managed browser service).
#     This is the EXPENSIVE fallback method - only use when API fails.

#     Features:
#     - Bypasses Cloudflare automatically
#     - Handles JavaScript rendering
#     - Solves CAPTCHAs automatically
#     - Uses real browser fingerprints

#     Args:
#         url: URL to fetch
#         timeout_ms: Request timeout in milliseconds

#     Returns:
#         HTML content as bytes, or None if failed

#     Requires BRIGHTDATA_BROWSER_URL in environment variables.
#     Format: wss://brd-customer-hl_XXXXX-zone-scraping_browser1:PASSWORD@brd.superproxy.io:9222
#     """
#     from playwright.sync_api import sync_playwright

#     browser_url = os.getenv('BRIGHTDATA_BROWSER_URL')

#     if not browser_url:
#         print("[brightdata-browser] ERROR: BRIGHTDATA_BROWSER_URL not found")
#         return None

#     try:
#         print(f"[brightdata-browser] Fetching {url} via Scraping Browser (expensive)...")
#         print(f"[brightdata-browser] Connecting to managed browser...")

#         with sync_playwright() as p:
#             browser = p.chromium.connect_over_cdp(browser_url)

#             contexts = browser.contexts
#             if contexts:
#                 context = contexts[0]
#             else:
#                 context = browser.new_context()

#             page = context.new_page()

#             print(f"[brightdata-browser] Navigating to {url}")
#             try:
#                 page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
#                 print(f"[brightdata-browser] Page loaded successfully")
#             except Exception as nav_err:
#                 print(f"[brightdata-browser] Navigation warning: {nav_err}")

#             try:
#                 title = page.title()
#                 print(f"[brightdata-browser] Page title: {title[:100]}")
#             except Exception as e:
#                 print(f"[brightdata-browser] Could not get page title: {e}")

#             try:
#                 page.wait_for_load_state("networkidle", timeout=10000)
#             except Exception:
#                 print(f"[brightdata-browser] Network idle timeout - continuing anyway")

#             html = page.content()
#             print(f"[brightdata-browser] Got {len(html)} chars")

#             if 'challenge' in html.lower()[:1000] or 'just a moment' in html.lower()[:1000]:
#                 print(f"[brightdata-browser] WARNING: Still got Cloudflare challenge")
#             else:
#                 print(f"[brightdata-browser] No Cloudflare challenge detected")

#             browser.close()

#             return html.encode("utf-8", "ignore")

#     except Exception as e:
#         print(f"[brightdata-browser] Failed to fetch {url}: {e}")
#         return None

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Test Bright Data crawler')
    parser.add_argument('--mode', type=str, choices=['html', 'facebook'], default='html',
                       help='Test mode: html (Unlocker API) or facebook (Scraper API)')
    parser.add_argument('--url', type=str, default='https://phnompenhpost.com', help='URL to test (for html mode)')
    parser.add_argument('--fb-url', type=str, help='Facebook page URL (for facebook mode)')
    parser.add_argument('--num-posts', type=int, default=10, help='Number of posts to fetch (for facebook mode)')
    parser.add_argument('--start-date', type=str, default='', help='Start date MM-DD-YYYY (for facebook mode)')
    parser.add_argument('--end-date', type=str, default='', help='End date MM-DD-YYYY (for facebook mode)')
    args = parser.parse_args()

    if args.mode == 'facebook':
        # Test Facebook Scraper API
        print("\n" + "="*70)
        print("BRIGHT DATA CRAWLER TEST - Facebook Scraper API")
        print("="*70)

        fb_url = args.fb_url or "https://www.facebook.com/LeBron/"
        print(f"\nFetching Facebook posts from: {fb_url}")
        print(f"Number of posts: {args.num_posts}")
        if args.start_date:
            print(f"Date range: {args.start_date} to {args.end_date}")

        result = fetch_facebook_posts(
            facebook_urls=[fb_url],
            num_of_posts=args.num_posts,
            start_date=args.start_date,
            end_date=args.end_date
        )

        if result:
            print(f"\n SUCCESS! Got {len(result)} result object(s) from Bright Data")

            # Save result to file
            output_file = "brightdata_facebook_output.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f" Saved result to: {output_file}")

            # Display summary of each result
            print(f"\nResponse summary:")
            for i, obj in enumerate(result):
                print(f"\n--- Object {i+1}/{len(result)} ---")
                # Show first 500 chars of each object
                obj_str = json.dumps(obj, indent=2, ensure_ascii=False)
                print(obj_str[:500])
                if len(obj_str) > 500:
                    print("... (truncated)")
        else:
            print(f"\n FAILED to fetch Facebook posts")
            print(f"\nMake sure you have:")
            print(f"  - BRIGHTDATA_API_KEY in .env")
            print(f"  - Dataset ID: gd_lkaxegm826bjpoo9m5 (hardcoded)")

    else:
        # Test HTML Unlocker API
        print("\n" + "="*70)
        print("BRIGHT DATA CRAWLER TEST - Unlocker API Only")
        print("="*70)

        test_url = args.url

        print(f"\nFetching {test_url}...")
        print("Strategy: API only (cheap, fast)")

        html = fetch_html_with_api(test_url)

        if html and len(html) > 1000:
            print(f"\n SUCCESS! Got {len(html)} bytes")

            # Save HTML to file
            output_file = "brightdata_test_output.html"
            with open(output_file, 'wb') as f:
                f.write(html)
            print(f" Saved HTML to: {output_file}")

            # Check if we got real content
            html_str = html.decode('utf-8', 'ignore')
            html_lower = html_str.lower()

            if 'challenge' not in html_lower[:1000] and 'just a moment' not in html_lower[:1000]:
                print(f" Real content detected (no Cloudflare challenge)")
            else:
                print(f" WARNING: Might still be a challenge page")

            print(f"\nFirst 500 chars of content:")
            print(html_str[:500])

            # Test cache
            print(f"\n\nTesting cache...")
            html2 = fetch_html_with_api(test_url)
            if html2 == html:
                print(f" Cache working! Got same content without making new request")
        else:
            print(f"\n FAILED: Got {len(html) if html else 0} bytes")
            print(f"\nMake sure you have:")
            print(f"  - BRIGHTDATA_UNLOCKER_API_KEY in .env")
            print(f"  - BRIGHTDATA_UNLOCKER_ZONE in .env (default: web_unlocker1)")

    print("\n" + "="*70)
    print("\nUsage examples:")
    print("  HTML mode (default):     python crawler_brightdata.py")
    print("  Custom URL:              python crawler_brightdata.py --url https://example.com")
    print("  Facebook mode:           python crawler_brightdata.py --mode facebook")
    print("  Facebook with options:   python crawler_brightdata.py --mode facebook --fb-url https://www.facebook.com/LeBron/ --num-posts 50")
    print("  Facebook with dates:     python crawler_brightdata.py --mode facebook --start-date 01-01-2025 --end-date 02-28-2025")
    print("="*70)
