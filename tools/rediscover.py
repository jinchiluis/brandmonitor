#!/usr/bin/env python3
"""Check pinned sitemaps against a full walk, and report what the pins would miss.

``sitemap_urls`` stops collection rediscovering a publisher's sitemap tree nine
times a day, which is the whole point: faz.net answered 100 files a pass of which
95 held nothing. The cost is that the pins are a standing bet - a file that keeps
returning 200 and valid XML but has quietly stopped carrying a section fails
nothing, because there is no longer a second file that happens to list the article
anyway.

So this does once a month, deliberately, what the daily pass no longer does: walk
the whole tree with ``collect_from_sitemaps`` and diff it against what the pins
return for the same window. A file that contributed an in-window article no pinned
file returned is the answer - either a new pin, or a publisher change worth
knowing about. The independent canaries in ``health/canary.py`` cover the faster
version of the same drift; this covers the slow one and says which file to add.

Read-only: it opens no database, writes no watermark, and stores nothing. It is a
heavy, polite operation - one full traversal per pinned source - so run it monthly
rather than on a schedule.

    python tools/rediscover.py
    python tools/rediscover.py --organization FAZ --days 3
    python tools/rediscover.py --json data/rediscover-2026-10-01.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src.collect import is_furniture, is_malformed, slug_for, url_is_excluded  # noqa: E402
from src.discovery import collect_from_pinned, has_pins  # noqa: E402
from src.logger import get_logger  # noqa: E402
from src.polite_http import PoliteAdapter  # noqa: E402
from src.probe import FileRecorder, file_yield  # noqa: E402
from vendor.newscrawler.crawler import (BERLIN_TZ, SITEMAP_HEADERS,  # noqa: E402
                                        collect_from_sitemaps, normalize_url)
from vendor.newscrawler.source_loader import sources  # noqa: E402

logger = get_logger(__name__)

DEFAULT_SOURCES = ROOT / "input" / "germany_medias.json"


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(SITEMAP_HEADERS)
    adapter = PoliteAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _kept(urls, entry: Dict[str, Any]) -> set:
    """Apply the filters that run after discovery, so both sides count the same."""
    return {normalize_url(url) for url in urls
            if not url_is_excluded(url, entry) and not is_furniture(url)
            and not is_malformed(url)}


def check_source(entry: Dict[str, Any], since: datetime, end: datetime,
                 max_per_source: int = 2000) -> Dict[str, Any]:
    """Walk one source's whole tree and diff it against its pins."""
    result: Dict[str, Any] = {"slug": slug_for(entry), "organization": entry.get("organization"),
                              "pinned": 0, "walked": 0, "files": 0, "missing": {},
                              "only_pinned": 0, "error": None}

    pinned_urls: set = set()
    try:
        hints = collect_from_pinned(entry, since, end, session=_session())
        pinned_urls = _kept((h.url for h in hints), entry)
    except Exception as exc:  # noqa: BLE001 - a broken pin is the finding, not a crash
        result["error"] = f"pins: {type(exc).__name__}: {exc}"

    recorder = FileRecorder()
    try:
        collect_from_sitemaps(_session(), entry["url"], since, end,
                              max_per_source=max_per_source, cache=recorder)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{result['error'] + '; ' if result['error'] else ''}" \
                          f"walk: {type(exc).__name__}: {exc}"
        return result

    per_file = file_yield(recorder, entry, since, end)
    walked: set = set()
    for url, row in per_file.items():
        kept = _kept(row["kept"], entry)
        walked |= kept
        missed = sorted(kept - pinned_urls)
        if missed:
            result["missing"][url] = missed

    result["pinned"] = len(pinned_urls)
    result["walked"] = len(walked)
    result["files"] = len(per_file)
    # Normal rather than alarming: a news sitemap robots.txt does not declare is
    # reachable only because it is pinned, which is often why it was pinned.
    result["only_pinned"] = len(pinned_urls - walked)
    return result


def format_result(result: Dict[str, Any]) -> List[str]:
    lines = [f"{result['organization'] or result['slug']} ({result['slug']})"]
    if result["error"]:
        lines.append(f"  ERROR {result['error']}")
    missing_total = sum(len(urls) for urls in result["missing"].values())
    lines.append(f"  pinned {result['pinned']}  walked {result['walked']} "
                 f"across {result['files']} file(s)  "
                 f"missing {missing_total}  pinned-only {result['only_pinned']}")
    for url, urls in sorted(result["missing"].items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  + {len(urls):>3} URL(s) only in {url}")
        for missed in urls[:3]:
            lines.append(f"        {missed}")
        if len(urls) > 3:
            lines.append(f"        ... and {len(urls) - 3} more")
    return lines


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES),
                        help="source list to check (default: input/germany_medias.json)")
    parser.add_argument("--days", type=float, default=2.0,
                        help="window to compare over, in days (default: 2)")
    parser.add_argument("--organization", default=None,
                        help="check only this organization")
    parser.add_argument("--max", type=int, default=2000,
                        help="max URLs per source for the walk (default: 2000)")
    parser.add_argument("--json", default=None, help="also write the result as JSON")
    args = parser.parse_args(argv)

    sources.clear_cache()
    entries = sources.load_sources(args.sources)
    if args.organization:
        entries = [e for e in entries
                   if (e.get("organization") or "").lower() == args.organization.lower()]
    pinned = [e for e in entries if has_pins(e)]
    if not pinned:
        print("No pinned sources in this list - nothing to check.")
        return 0

    end = datetime.now(tz=BERLIN_TZ)
    since = end - timedelta(days=args.days)
    print(f"Comparing pins against a full walk, {since:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}")
    print(f"{len(pinned)} pinned source(s) of {len(entries)}\n")

    results = []
    for entry in pinned:
        logger.info("[rediscover] walking %s", entry["url"])
        result = check_source(entry, since, end, max_per_source=args.max)
        results.append(result)
        print("\n".join(format_result(result)))
        print()

    if args.json:
        Path(args.json).write_text(
            json.dumps({"since": since.isoformat(), "until": end.isoformat(),
                        "results": results}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"Wrote {args.json}")

    drifted = [r for r in results if r["missing"] or r["error"]]
    if drifted:
        print(f"{len(drifted)} source(s) need attention: "
              f"{', '.join(r['slug'] for r in drifted)}")
        return 1
    print("Every pinned source's pins cover everything the full walk found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
