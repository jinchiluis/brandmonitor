"""News collection: run the vendored crawler over a source list and store results.

This is the collection half of the first vertical slice. It deliberately stops at
storing normalised source material — no client relevance is applied here, per the
design rule that collection is source-specific and analysis is client-specific.

What it guarantees, because mvp_plan requires it:
  * every configured source reports ok / zero / failed, never silence;
  * a source that raises does not stop the others;
  * re-running the same window stores no duplicates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

from src.config import CRAWLER_WORKERS, INPUT_DIR, NEWS_LOOKBACK_DAYS
from src.db import (
    finish_run, get_watermark, record_source_result, session, set_watermark,
    start_run, utcnow,
)
from src.logger import get_logger
from vendor.newscrawler.crawler import (
    BERLIN_TZ, HTML_HEADERS, SITEMAP_HEADERS, ArticleHint,
    collect_from_feeds, collect_from_frontpage, collect_from_sitemaps,
    in_range, normalize_url, pick_accessible_origin,
)
from vendor.newscrawler.source_loader import sources

logger = get_logger(__name__)

DEFAULT_NEWS_SOURCES = INPUT_DIR / "germany_medias.json"
DEFAULT_REGULATORY_SOURCES = INPUT_DIR / "regulatory_sources.json"

# Sections whose shallow pages list other articles rather than being one: tag,
# author and topic indexes. They carry no article, and they change whenever
# anything they list changes, so they never stop looking new.
#
# allowed_dirs cannot express this. It filters by path prefix, so on BVL no prefix
# keeps /blog/<post> while dropping /blog/tag/<term>. Depth is what separates them:
# /themen/marktplaetze is an index, /themen/marktplaetze/<slug> is an article.
INDEX_MARKERS = frozenset({
    "tag", "tags", "thema", "themen", "schlagwort", "autor", "autoren",
    "author", "authors", "experten", "category", "kategorie", "rubrik",
})

# Paginated listings: /page/2, /blog/page/7. The trailing number is what
# separates them from a real section that happens to be called "page".
PAGINATION_MARKERS = frozenset({"page", "seite"})

# CMS plumbing and uploaded assets. Never an article.
ASSET_MARKERS = frozenset({"wp-content", "wp-json", "wp-admin", "wp-includes"})

# Characters RFC 3986 does not allow unescaped in a path. A URL carrying one did
# not come from a working template - see is_malformed.
UNSAFE_PATH_CHARS = ('"', "<", ">", "{", "}", "|", "\\", "^", "`", "$(")


def is_furniture(url: str) -> bool:
    """True for index pages that list articles instead of being one.

    The marker may sit one level down: BVL nests them under its blog
    (/blog/tag/<term>), and matching only the first segment kept 898 tag, author
    and category pages while the docstring claimed otherwise.

    Depth 1 is the limit, and that limit is load-bearing rather than arbitrary.
    FAZ has a real content section named ``autoren`` at
    /aktuell/feuilleton/buecher/autoren/<article-slug>, so searching every
    segment silently drops 21 genuine articles. An index marker deep in a path
    is a section name; near the front it is a listing.
    """
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return True  # a bare homepage is never an article
    lowered = [s.lower() for s in segments]

    for i, seg in enumerate(lowered[:2]):
        # "at most one thing below the marker" is what makes it a listing:
        # /blog/tag is the index, /blog/tag/zoll is one term's index, but
        # /blog/<slug> is a post.
        if seg in INDEX_MARKERS and len(segments) - i <= 2:
            return True

    for i, seg in enumerate(lowered[:-1]):
        if seg in PAGINATION_MARKERS and lowered[i + 1].isdigit():
            return True

    if any(seg in ASSET_MARKERS for seg in lowered):
        return True

    # WordPress comment permalinks are the same post under another URL.
    if "replytocom" in parse_qs(urlparse(url).query):
        return True

    return False


def is_malformed(url: str) -> bool:
    """True for URLs that cannot address a real page.

    BVL's sitemap ships 816 URLs with a JavaScript fragment appended -
    ``/blog/tag/zoll/"%20+%20$(%20img%20)%20.%20attr(`` - from a template that
    concatenated markup into a link. They are not index pages, so the furniture
    rule has no business catching them, and each one otherwise costs a body
    fetch against a URL that was never a page. No other configured source
    produces any, so this drops nothing real.
    """
    path = unquote(urlparse(url).path)
    return any(c in path for c in UNSAFE_PATH_CHARS)


def slug_for(entry: Dict[str, Any]) -> str:
    """Stable per-source key. Derived from the domain so it survives renaming."""
    host = urlparse(entry["url"]).netloc.lower()
    return host.removeprefix("www.")


def _hash(hint: ArticleHint) -> str:
    basis = f"{normalize_url(hint.url)}|{hint.title or ''}|{hint.published_at or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _dedupe_hints(hints: List[ArticleHint]) -> List[ArticleHint]:
    """Collapse hints that point at the same article.

    A source with several discovery methods finds the same URL through each of
    them, and the copies disagree: plain sitemaps carry no title where feeds do,
    and <lastmod> and <pubDate> rarely match. Those differences read as a changed
    article at storage time, so without this the eight sources configured with
    both methods would store every article twice. Keep the richest copy - one
    with a title first, then one with a date.
    """
    best: Dict[str, ArticleHint] = {}
    for h in hints:
        key = normalize_url(h.url)
        current = best.get(key)
        if current is None or (h.title is not None, h.published_at is not None) > (
                current.title is not None, current.published_at is not None):
            best[key] = h
    return list(best.values())


def collect_source(entry: Dict[str, Any], start: datetime,
                   end: datetime, max_per_source: int) -> Tuple[List[ArticleHint], Optional[str]]:
    """Run every enabled discovery method for one source.

    Returns (hints, error). An error from one method is fatal for that source only;
    it is surfaced rather than swallowed, because a silently failing source looks
    exactly like a quiet one.
    """
    hints: List[ArticleHint] = []
    errors: List[str] = []
    session_html = requests.Session()
    session_html.headers.update(HTML_HEADERS)
    try:
        origin = pick_accessible_origin(session_html, entry["url"])
        session_html.headers["Referer"] = origin + "/"
    except Exception as exc:
        # Nothing can run without an origin, so this one is genuinely fatal.
        return [], f"origin probe failed: {type(exc).__name__}: {exc}"

    # Each method runs independently. A failing sitemap must not cost us the feed:
    # the methods are alternative routes to the same site, not a pipeline.
    if entry.get("sitemap"):
        sm = requests.Session()
        sm.headers.update(SITEMAP_HEADERS)
        try:
            hints += collect_from_sitemaps(sm, entry["url"], start, end,
                                           max_per_source=max_per_source)
        except Exception as exc:
            errors.append(f"sitemap: {type(exc).__name__}: {exc}")

    if entry.get("feeds"):
        try:
            feed_hints = collect_from_feeds(session_html, origin)
            # Feeds are not window-limited by the collector, so filter here.
            hints += [h for h in feed_hints
                      if h.published_at is None or in_range(h.published_at, start, end)]
        except Exception as exc:
            errors.append(f"feeds: {type(exc).__name__}: {exc}")

    if entry.get("frontpage"):
        try:
            hints += collect_from_frontpage(session_html, origin,
                                            start_date=start, cap=300)
        except Exception as exc:
            errors.append(f"frontpage: {type(exc).__name__}: {exc}")

    return _dedupe_hints(hints), "; ".join(errors) if errors else None


def store_hints(conn: sqlite3.Connection, run_id: int, slug: str,
                hints: List[ArticleHint], source_kind: str = "news", *,
                full_text: bool = False) -> int:
    """Insert hints as raw items. Returns the number of rows written.

    For title-only sources, a hash matching any stored version is skipped. One whose
    hash is new - a retitled or re-dated article - is written as a further version
    rather than dropped, which is what mvp_plan means by "versioned raw source
    items". Earlier versions are left intact. Full-text sources instead queue a
    body recheck and let the body stage decide whether content has changed.
    """
    from src.bodies import queue_body

    stored = 0
    fetched = utcnow()
    unique = _dedupe_hints(hints)
    kept = [h for h in unique if not is_furniture(h.url) and not is_malformed(h.url)]
    if len(kept) < len(unique):
        furniture = sum(1 for h in unique if is_furniture(h.url))
        malformed = sum(1 for h in unique
                        if is_malformed(h.url) and not is_furniture(h.url))
        logger.info("[collect] %s: dropped %d index page(s) and %d malformed URL(s)",
                    slug, furniture, malformed)
    for h in kept:
        external_id = normalize_url(h.url)
        content_hash = _hash(h)
        published = h.published_at.isoformat() if h.published_at else None
        payload_data = {
            "url": external_id,
            "title": h.title,
            "published_at": published,
            "discovered_via": h.source,
        }

        # Every stored version is compared, not just the newest. Two
        # representations of one article that alternate, or a CMS that re-stamps
        # <lastmod> on a page already seen, each look "changed" relative to the
        # other - and comparing only the newest let that ratchet a fresh version
        # on every run, without bound.
        prior = conn.execute(
            "SELECT version, content_hash FROM raw_item "
            "WHERE source_slug = ? AND external_id = ?",
            (slug, external_id),
        ).fetchall()
        if full_text:
            queue_body(conn, slug, external_id, payload_data)
            # Discovery metadata requests a body recheck. It must neither version
            # a CMS date change nor replace an enriched item with a headline stub.
            if prior:
                continue
            payload_data["body_status"] = "pending"
        if any(r["content_hash"] == content_hash for r in prior):
            continue
        version = max(r["version"] for r in prior) + 1 if prior else 1

        payload = json.dumps(payload_data, ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_item "
            "(source_slug, source_kind, external_id, version, url, title, "
            " published_at, fetched_at, first_run_id, content_hash, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, source_kind, external_id, version, external_id, h.title,
             published, fetched, run_id, content_hash, payload),
        )
        stored += cur.rowcount
    return stored


def run_collection(sources_path: Optional[Path] = None, *, days: Optional[float] = None,
                   kind: str = "news", max_per_source: int = 2000,
                   workers: Optional[int] = None, db_path: Optional[Path] = None,
                   body_limit: Optional[int] = None) -> Dict[str, Any]:
    """Collect every source in a list into the database. Returns a summary dict."""
    from src.bodies import content_mode, run_body_fetch
    from src.config import BODY_FETCH_LIMIT

    limit = BODY_FETCH_LIMIT if body_limit is None else body_limit
    if limit < 1:
        raise ValueError("body fetch limit must be positive")
    path = Path(sources_path) if sources_path else (
        DEFAULT_REGULATORY_SOURCES if kind == "regulatory" else DEFAULT_NEWS_SOURCES)
    sources.clear_cache()
    entries = sources.load_sources(str(path))
    if not entries:
        raise ValueError(f"no sources in {path}")
    modes = {slug_for(entry): content_mode(entry) for entry in entries}

    end = datetime.now(tz=BERLIN_TZ)
    scope = f"collection:{kind}"

    with session(db_path) as conn:
        mark = get_watermark(conn, scope)
    if days is not None:
        start = end - timedelta(days=days)
    elif mark:
        start = datetime.fromisoformat(mark)
    else:
        start = end - timedelta(days=NEWS_LOOKBACK_DAYS)

    # An explicit --days window that starts after the watermark leaves a hole.
    # Advancing the watermark over it would make that hole permanent and silent,
    # so such a run collects normally but does not move the mark.
    leaves_gap = bool(mark) and start > datetime.fromisoformat(mark)
    if leaves_gap:
        logger.warning(
            "[collect] window starts %s but watermark is %s - %s of history would be "
            "skipped, so the watermark will not advance",
            start.isoformat(), mark, start - datetime.fromisoformat(mark))

    logger.info("[collect] %s: %d sources, %s -> %s",
                kind, len(entries), start.isoformat(), end.isoformat())

    def work(entry):
        slug = slug_for(entry)
        try:
            hints, error = collect_source(entry, start, end, max_per_source)
        except Exception as exc:  # noqa: BLE001 - one source must not end the run
            return entry, slug, [], f"{type(exc).__name__}: {exc}"
        return entry, slug, hints, error

    results = []
    with ThreadPoolExecutor(max_workers=workers or CRAWLER_WORKERS) as ex:
        for r in ex.map(work, entries):
            results.append(r)

    summary = {"kind": kind, "sources": len(entries), "ok": 0, "zero": 0,
               "failed": 0, "found": 0, "stored": 0, "start": start.isoformat(),
               "end": end.isoformat(), "per_source": []}

    with session(db_path) as conn:
        run_id = start_run(conn, kind, start.isoformat(), end.isoformat())
        for entry, slug, hints, error in results:
            # Store whatever was collected even when a method failed. Discarding a
            # source's successful sitemap sweep because its feed timed out loses
            # real data and is invisible afterwards.
            stored = store_hints(conn, run_id, slug, hints, source_kind=kind,
                                 full_text=modes[slug] == "full_text")
            if error:
                status = "failed"
                summary["failed"] += 1
                logger.warning("[collect] %s FAILED (%d hints kept): %s",
                               slug, len(hints), error)
            else:
                status = "ok" if hints else "zero"
                summary["ok" if hints else "zero"] += 1
            summary["found"] += len(hints)
            summary["stored"] += stored
            summary["per_source"].append(
                {"slug": slug, "organization": entry.get("organization"),
                 "status": status, "found": len(hints), "stored": stored,
                 "error": error})
            record_source_result(conn, run_id, slug, status,
                                 items_found=len(hints), items_stored=stored,
                                 error=error)

        # Only advance the watermark when nothing failed and the window left no
        # hole: a partial window that looks complete is how gaps become permanent.
        if summary["failed"] == 0 and not leaves_gap:
            set_watermark(conn, scope, end.isoformat())
        elif summary["failed"]:
            logger.warning("[collect] %d source(s) failed - watermark not advanced",
                           summary["failed"])
        summary["watermark_advanced"] = summary["failed"] == 0 and not leaves_gap
        finish_run(conn, run_id, "ok" if summary["failed"] == 0 else "failed",
                   note=f"{summary['stored']} new items")
        summary["run_id"] = run_id

    # Discovery is committed before any page fetch. The durable body queue is
    # independent of the discovery watermark, including for feed items that vanish.
    if "full_text" in modes.values():
        summary["bodies"] = run_body_fetch(path, kind=kind, limit=limit, db_path=db_path)
    return summary
