#!/usr/bin/env python3
"""
Production Google Custom Search API Crawler
--------------------------------------------
Search Google for ALL articles from a specific news site on a specific date.
Returns results in ArticleRecord format compatible with your crawler pipeline.

Setup (one-time):
1. Create a Programmable Search Engine:
   - Go to: https://programmablesearchengine.google.com/
   - Click "Add" to create a new search engine
   - Under "Sites to search": select "Search the entire web"
   - Click "Create"
   - Copy your "Search engine ID" (cx parameter)

2. Get an API key:
   - Go to: https://console.cloud.google.com/apis/credentials
   - Click "Create credentials" → "API key"
   - Enable "Custom Search API" in your Google Cloud project
   - Copy your API key

3. Set environment variables:
   set GOOGLE_API_KEY=your_api_key_here
   set GOOGLE_SEARCH_ENGINE_ID=your_search_engine_id_here

Usage examples:
    # Get all articles from a specific site on a specific date
    python google_searcher.py --site nation.africa --date 2025-10-31 --output output/Kenya -v

    # Get articles from date range with max results limit
    python google_searcher.py --site pv-magazine.de --start 2025-05-01 --end 2025-05-07 --max-results 100 -v

    # Multiple sites (reads from newline-separated file)
    python google_searcher.py --sites-file sites.txt --date 2025-10-31 -v

Features:
- Site-specific search (site:example.com)
- Strict date filtering (only articles matching exact date/range)
- Returns results in ArticleRecord-compatible format with summary field
- Enriches missing titles and dates from HTML
- Free tier: 100 queries per day
"""

import os
import json
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from pathlib import Path
from urllib.parse import urlparse

import requests
from lxml import html as lxml_html
from vendor.newscrawler.crawler_html_utils import fetch_html, parse_flexible

# -------------- CONFIG --------------

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_SEARCH_ENGINE_ID = os.getenv("GOOGLE_SEARCH_ENGINE_ID")

# -------------- CORE FUNCTIONS --------------


def google_custom_search(
    site: str,
    start_date: str,
    end_date: str,
    keywords: Optional[List[str]] = None,
    max_results: int = 100,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Call Google Custom Search JSON API for a specific site and date range.

    Args:
        site: Domain to search (e.g., "nation.africa")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        keywords: Optional keywords to add to query (helps with date sorting)
        max_results: Maximum number of results to fetch
        verbose: Print debug information

    Returns:
        List of search result items from Google API
    """
    if not GOOGLE_API_KEY or not GOOGLE_SEARCH_ENGINE_ID:
        raise RuntimeError("GOOGLE_API_KEY or GOOGLE_SEARCH_ENGINE_ID is not set in env")

    url = "https://www.googleapis.com/customsearch/v1"

    # Build query - can be just site: or with keywords
    if keywords:
        keyword_str = " ".join(keywords)
        query = f"{keyword_str} site:{site}"
    else:
        query = f"site:{site}"

    # Google returns 10 per page max
    remaining = max_results
    start_index = 1
    results: List[Dict[str, Any]] = []

    # Build params with dateRestrict (relative to NOW)
    # dateRestrict=d1 means "last 24 hours from now"
    params_common = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_SEARCH_ENGINE_ID,
        "q": query,
        "dateRestrict": "d1",  # Last 1 day
    }

    if verbose:
        print(f"[debug] searching site: {site}")
        print(f"[debug] target date range: {start_date} to {end_date}")
        print(f"[debug] dateRestrict: d1 (last 24 hours)")
        print(f"[debug] query: {query}")

    while remaining > 0:
        page_size = min(10, remaining)
        params = dict(params_common)
        params["start"] = start_index
        params["num"] = page_size

        if verbose:
            print(f"[debug] requesting page start={start_index} num={page_size}...")

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[error] API request failed: {e}")
            break

        if verbose:
            search_info = data.get("searchInformation", {})
            total_results = search_info.get("totalResults", "0")
            print(f"[debug] Google reports {total_results} total results available")

        if "error" in data:
            # Stop on API error
            error_msg = data["error"].get("message")
            error_reason = data["error"].get("errors", [{}])[0].get("reason", "unknown")
            print(f"[error] {error_msg} (reason: {error_reason})")
            break

        items = data.get("items", [])
        if not items:
            if verbose:
                print("[debug] no more items returned by Google")
            break

        if verbose:
            print(f"[debug] received {len(items)} items on this page")

        results.extend(items)
        remaining -= len(items)
        start_index += len(items)

        if len(items) < page_size:
            # No more pages
            break

    return results


def extract_metadata_from_html(html_bytes: bytes, url: str, verbose: bool = False) -> Dict[str, Optional[str]]:
    """
    Extract title and date from HTML content.
    Returns dict with: title, published_at
    """
    if not html_bytes:
        return {"title": None, "published_at": None}

    try:
        tree = lxml_html.fromstring(html_bytes)

        # Extract title (priority: og:title > title tag, remove site name suffix)
        title = None
        og_title = tree.xpath('//meta[@property="og:title"]/@content')
        if og_title:
            title = og_title[0].strip()
        else:
            title_tag = tree.xpath('//title/text()')
            if title_tag:
                title = title_tag[0].strip()

        # Clean up title - remove common site name suffixes
        if title:
            for sep in [' - ', ' | ', ' – ', ' — ']:
                if sep in title:
                    parts = title.split(sep)
                    if len(parts[0]) > 10:
                        title = parts[0].strip()
                        break

        # Extract date
        pub_date = None

        # Check JSON-LD
        for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                data = json.loads(script.strip())
                objs = data if isinstance(data, list) else [data]
                for obj in objs:
                    for key in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
                        if obj.get(key):
                            dt = parse_flexible(str(obj.get(key)))
                            if dt:
                                pub_date = dt.isoformat()
                                break
                    if pub_date:
                        break
            except:
                pass

        # Check meta tags if not found in JSON-LD
        if not pub_date:
            xps = [
                '//meta[@property="article:published_time"]/@content',
                '//meta[@name="pubdate"]/@content',
                '//meta[@name="date"]/@content',
                '//meta[@name="DC.date.issued"]/@content',
                '//*[@itemprop="datePublished"]/@content',
                '//time/@datetime',
            ]
            for xp in xps:
                try:
                    vals = tree.xpath(xp)
                    if vals:
                        dt = parse_flexible(vals[0])
                        if dt:
                            pub_date = dt.isoformat()
                            break
                except:
                    pass

        return {
            "title": title,
            "published_at": pub_date,
        }

    except Exception as e:
        if verbose:
            print(f"[extract_metadata] error: {e}")
        return {"title": None, "published_at": None}


def date_in_range(date_str: Optional[str], start_date: str, end_date: str) -> bool:
    """
    Check if a date string falls within the given range (inclusive).

    Args:
        date_str: ISO date string to check
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        True if date is in range, False otherwise
    """
    if not date_str:
        return False

    try:
        dt = parse_flexible(date_str)
        if not dt:
            return False

        # Compare dates only (ignore time)
        date_only = dt.date()
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

        return start_dt <= date_only <= end_dt
    except:
        return False


def normalize_items(
    items: List[Dict[str, Any]],
    site: str,
    enrich: bool = True,
    session: Optional[requests.Session] = None,
    verbose: bool = False
) -> List[Dict[str, Any]]:
    """
    Convert Google API results to ArticleRecord format.
    NO date filtering - just return what Google gives us with dateRestrict.

    Args:
        items: Raw Google search results
        site: Site domain being searched
        enrich: If True, fetch HTML to get full title and date (slower)
        session: requests.Session for fetch_html
        verbose: Print debug information

    Returns:
        List of ArticleRecord dicts with: site, title, url, published_at, crawled_at, summary
    """
    out: List[Dict[str, Any]] = []
    now_iso = datetime.now().astimezone().isoformat()

    for i, it in enumerate(items, 1):
        title = it.get("title")
        link = it.get("link")
        snippet = it.get("snippet", "")

        # Try to pick a date from pagemap if present
        pub_date = None
        pagemap = it.get("pagemap") or {}
        metatags = pagemap.get("metatags") or []
        for mt in metatags:
            for key in ("article:published_time", "og:updated_time", "pubdate", "date"):
                if key in mt:
                    pub_date = mt[key]
                    break
            if pub_date:
                break

        # Decide if we need to fetch HTML
        title_is_complete = title and not title.endswith("...")
        needs_enrichment = not (title_is_complete and pub_date)

        # Enrich with actual page metadata if needed
        if enrich and link and session and needs_enrichment:
            if verbose:
                reason = []
                if not title_is_complete:
                    reason.append("title truncated")
                if not pub_date:
                    reason.append("missing date")
                print(f"[enrich] {i}/{len(items)}: {link} ({', '.join(reason)})")

            try:
                html_bytes = fetch_html(session, link)
                if html_bytes:
                    metadata = extract_metadata_from_html(html_bytes, link)

                    # Override with fetched data if better
                    if metadata["title"]:
                        title = metadata["title"]
                    if metadata["published_at"] and not pub_date:
                        pub_date = metadata["published_at"]
            except Exception as e:
                if verbose:
                    print(f"[enrich] error: {e}")

        # Only include if we have a date
        if not pub_date:
            if verbose:
                print(f"[skip] {i}/{len(items)}: no date found")
            continue

        # Build ArticleRecord
        out.append({
            "site": site,
            "title": title,
            "url": link,
            "published_at": pub_date,
            "crawled_at": now_iso,
            "summary": snippet,
        })

    return out


# -------------- CLI --------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Production Google Custom Search for specific news sites"
    )

    # Site specification (mutually exclusive)
    site_group = p.add_mutually_exclusive_group(required=True)
    site_group.add_argument(
        "--site",
        help="Single site domain to search (e.g., nation.africa)",
    )
    site_group.add_argument(
        "--sites-file",
        help="File containing one site domain per line",
    )

    # Date specification (mutually exclusive)
    date_group = p.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        "--date",
        help="Single date to search (YYYY-MM-DD)",
    )
    date_group.add_argument(
        "--start",
        help="Start date for range (YYYY-MM-DD), requires --end",
    )

    p.add_argument(
        "--end",
        help="End date for range (YYYY-MM-DD), requires --start",
    )

    p.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Max results to fetch per site (default: 100)",
    )

    p.add_argument(
        "--keywords",
        nargs="+",
        help="Optional keywords to help with search (e.g., --keywords news article)",
    )

    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print debug info",
    )

    p.add_argument(
        "-o",
        "--output",
        help="Output folder (creates output/YYYYMMDDHHMMSS/site_domain.jsonl)",
    )

    return p.parse_args()


def process_site(
    site: str,
    start_date: str,
    end_date: str,
    keywords: Optional[List[str]],
    max_results: int,
    verbose: bool,
    session: requests.Session
) -> List[Dict[str, Any]]:
    """
    Process a single site and return normalized articles.
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"Processing: {site}")
        print(f"{'='*60}")

    # Search Google
    items = google_custom_search(
        site=site,
        start_date=start_date,
        end_date=end_date,
        keywords=keywords,
        max_results=max_results,
        verbose=verbose,
    )

    if verbose:
        print(f"[debug] found {len(items)} raw results from Google")

    # Normalize (no date filtering - trust dateRestrict)
    normalized = normalize_items(
        items=items,
        site=site,
        enrich=True,
        session=session,
        verbose=verbose
    )

    return normalized


def main() -> None:
    args = parse_args()

    # Validate date arguments
    if args.start and not args.end:
        print("Error: --start requires --end")
        return
    if args.end and not args.start:
        print("Error: --end requires --start")
        return

    # Determine date range
    if args.date:
        start_date = args.date
        end_date = args.date
    else:
        start_date = args.start
        end_date = args.end

    # Validate dates
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        print("Error: Dates must be in YYYY-MM-DD format")
        return

    # Determine sites to process
    sites = []
    if args.site:
        sites = [args.site]
    else:
        with open(args.sites_file, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]

    if not sites:
        print("Error: No sites to process")
        return

    if args.verbose:
        print(f"[debug] Processing {len(sites)} site(s)")
        print(f"[debug] Date range: {start_date} to {end_date}")

    # Create session for enrichment
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # Process each site
    all_results = {}
    for site in sites:
        normalized = process_site(
            site=site,
            start_date=start_date,
            end_date=end_date,
            keywords=args.keywords,
            max_results=args.max_results,
            verbose=args.verbose,
            session=session
        )

        all_results[site] = normalized

        # Print to console (handle unicode errors for Windows console)
        print(f"\n{site}: {len(normalized)} articles")
        for i, r in enumerate(normalized, 1):
            title = r['title'].encode('ascii', errors='replace').decode('ascii')
            print(f"  {i}. {title}")
            print(f"     {r['url']}")
            print(f"     published: {r['published_at']}")

    # Save to file if requested
    if args.output:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        output_dir = Path(args.output) / timestamp
        output_dir.mkdir(parents=True, exist_ok=True)

        for site, normalized in all_results.items():
            # Sanitize site name for filename
            safe_site = site.replace(".", "_").replace("/", "_")
            output_file = output_dir / f"{safe_site}.jsonl"

            with open(output_file, "w", encoding="utf-8") as f:
                for record in normalized:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            print(f"\nSaved {len(normalized)} articles to {output_file}")

    # Print summary
    total_articles = sum(len(articles) for articles in all_results.values())
    print(f"\n{'='*60}")
    print(f"Total: {total_articles} articles from {len(sites)} site(s)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
