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

import dataclasses
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

from src.config import (COLLECTION_OVERLAP_HOURS, CRAWLER_WORKERS, INPUT_DIR,
                        NEWS_LOOKBACK_DAYS)
from src.db import (
    advance_watermark, finish_run, get_watermark, record_source_result, session,
    set_watermark,
    start_run, utcnow,
)
from src.logger import get_logger
from src.polite_http import (DiscoveryCache, PoliteAdapter, load_discovery_cache,
                             save_discovery_cache)
from vendor.newscrawler.crawler import (
    BERLIN_TZ, HTML_HEADERS, SITEMAP_HEADERS, ArticleHint,
    collect_from_feeds, collect_from_frontpage, collect_from_sitemaps,
    in_range, normalize_url, pick_accessible_origin, url_matches_dirs,
)
from vendor.newscrawler.source_loader import sources

logger = get_logger(__name__)

DEFAULT_NEWS_SOURCES = INPUT_DIR / "germany_medias.json"
DEFAULT_REGULATORY_SOURCES = INPUT_DIR / "regulatory_sources.json"


def source_watermark_scope(kind: str, slug: str) -> str:
    """Return the independent discovery checkpoint for one configured source."""
    return f"collection:{kind}:{slug}"


def _legacy_source_start(conn: sqlite3.Connection, kind: str, slug: str,
                         global_mark: Optional[str]) -> Optional[str]:
    """Find a safe checkpoint for a source created before per-source watermarks.

    A source present in historical runs inherits the old global checkpoint. A
    genuinely new source must not inherit it, because doing so would skip its
    initial lookback. Databases that never completed a global run fall back to the
    earliest window in which that source was attempted.
    """
    rows = conn.execute(
        "SELECT r.window_start FROM run_source rs JOIN run r ON r.id=rs.run_id "
        "WHERE r.kind=? AND rs.source_slug=? AND r.window_start IS NOT NULL",
        (kind, slug),
    ).fetchall()
    if not rows:
        return None
    if global_mark:
        return global_mark
    return min(rows, key=lambda row: datetime.fromisoformat(row["window_start"]))[
        "window_start"]

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


def url_is_excluded(url: str, entry: Dict[str, Any], title: str | None = None) -> bool:
    """Return whether a source excludes this URL from every pipeline stage.

    Prefixes handle whole sections such as ``/fachmagazin``.  Some publishers put
    rolling indexes beside real articles under the same section, so an optional
    literal fragment list handles those without teaching generic furniture
    detection about one site's naming convention.

    Fragments see the query string as well as the path. BPEX serves its
    pagination (``/aktuelles?page_a12=2``) and PDF copies of its press releases
    (``/aktuelles?file=...``) on the bare section path, beside real items at
    ``/aktuelles/meldung/<slug>``; only the ``?`` tells them apart.
    """
    excluded = entry.get("excluded_dirs") or []
    if excluded and url_matches_dirs(url, excluded):
        return True
    fragments = entry.get("excluded_url_substrings") or []
    parts = urlparse(url)
    # Regular expressions over the path, for section indexes that share a prefix
    # with their articles: etailment's /magazin/ki beside /magazin/<slug-with-hyphens>.
    patterns = entry.get("excluded_url_patterns") or []
    if any(re.search(pattern, parts.path or "/") for pattern in patterns):
        return True
    target = unquote(parts.path + (f"?{parts.query}" if parts.query else "")).casefold()
    if any(str(fragment).casefold() in target for fragment in fragments):
        return True
    title_fragments = entry.get("excluded_title_substrings") or []
    folded_title = (title or "").casefold()
    return any(str(fragment).casefold() in folded_title
               for fragment in title_fragments)


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


def crawled_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Entries the vendored crawler and the body fetcher work on.

    An entry naming a ``collector`` is stored by that collector's own command.
    It stays in the source list so the selector and the body gates see its items,
    but crawling its site would record a zero-yield source every day, and
    bulk-fetching its stored URLs would read DIP's web app as an article.
    """
    return [entry for entry in entries if not entry.get("collector")]


DISCOVERY_METHODS = ("sitemap", "feeds", "frontpage", "brightdata")


def discovery_enabled(entry: Dict[str, Any]) -> bool:
    """True when any discovery method is switched on; a listed domain defaults off."""
    return any(entry.get(method) is True for method in DISCOVERY_METHODS)


def collector_entry(name: str, sources_path: Optional[Path] = None) -> Dict[str, Any]:
    """The regulatory source entry stored by the collector called ``name``."""
    path = Path(sources_path) if sources_path else DEFAULT_REGULATORY_SOURCES
    for entry in json.loads(path.read_text(encoding="utf-8")):
        if entry.get("collector") == name:
            return entry
    raise ValueError(f'{path} has no entry with "collector": "{name}"')


def on_configured_host(hint: ArticleHint, entry: Dict[str, Any]) -> ArticleHint:
    """Put a hint on the source's configured host when it differs only by ``www.``.

    ``pick_accessible_origin`` tries the configured host first but falls back to
    its www twin when the probe times out, and frontpage links resolve against
    whichever origin won. On 2026-09-13 one slow probe moved 19 BPEX releases from
    ``bpex-ev.de`` to ``www.bpex-ev.de``; the external id is the URL, so they were
    stored, body-gated and alerted as new items although they were from April.
    Measured that day, every web source stored only its configured host, so this
    rewrites no identity that already exists.
    """
    configured = urlparse(entry["url"]).netloc.lower()
    parts = urlparse(hint.url)
    host = parts.netloc.lower()
    if host == configured or host.removeprefix("www.") != configured.removeprefix("www."):
        return hint
    return dataclasses.replace(hint, url=parts._replace(netloc=configured).geturl())


def _hash(hint: ArticleHint) -> str:
    basis = f"{normalize_url(hint.url)}|{hint.title or ''}|{hint.published_at or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _title_key(title: Optional[str]) -> str:
    return " ".join((title or "").split())


def _is_retitle(hint: ArticleHint, prior: List[sqlite3.Row]) -> bool:
    """Whether a hint for an already stored title-only item is a new version.

    A title-only item's content is its title. Its discovery date is not content:
    WELT, WiWo and Handelsblatt move <news:publication_date> when they update an
    article, ZEIT moves its feed <pubDate>, BVL restamps <lastmod>, and SZ's
    frontpage invents 23:59:59 - about 500 bodyless versions a day, measured
    2026-09-15. So only a title no version has carried yet is a new version, and
    a missing title never is: it would replace a known title with a stub.

    Nothing discovered replaces a fetched body either. A frontpage re-date wrote
    version 3 over the fetched body of an SZ article about Temu and Shein, and
    every stage reads the latest version, so the body vanished for all of them.
    """
    title = _title_key(hint.title)
    if not title or any(row["has_body"] for row in prior):
        return False
    return all(_title_key(row["title"]) != title for row in prior)


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
                   end: datetime, max_per_source: int, *,
                   overlap: timedelta = timedelta(hours=COLLECTION_OVERLAP_HOURS),
                   truncation: Optional[Dict[str, Any]] = None,
                   cache_rows: Optional[Dict[str, Any]] = None,
                   http: Optional[Dict[str, Any]] = None,
                   ) -> Tuple[List[ArticleHint], Optional[str]]:
    """Run every enabled discovery method for one source.

    Returns (hints, error). An error from one method is fatal for that source only;
    it is surfaced rather than swallowed, because a silently failing source looks
    exactly like a quiet one.

    ``start`` is the source's watermark, but sitemap and feed dates are filtered
    from ``start - overlap``. A discovery date is not the moment an article became
    visible: DVZ and haendlerbund.de state date-only dates, read as 02:00, and
    VerkehrsRundschau lists articles hours after their <lastmod>. Filtering from
    the watermark itself lost every such article for good while the source
    reported ok. Re-found URLs cost nothing - store_hints skips them - so the
    overlap re-reads without re-storing. Frontpage results are not date-filtered
    and keep the unshifted start.

    When ``truncation`` is given, it receives ``cap`` ("url_cap" or "fetch_cap")
    and ``note`` if the sitemap traversal stopped at a cap. That is not an error:
    what was found is kept, but the source may have more.

    ``cache_rows`` are the source's stored sitemap and feed files
    (``load_discovery_cache``). When ``http`` is given it receives the pass's
    ``requests``, ``not_modified``, ``replayed`` and ``bytes`` counts and the
    ``cache_updates`` to save once the hints are stored. A host that throttles
    the pass (see ``PoliteAdapter``) makes the source an error, keeping what was
    found before it did.
    """
    since = start - overlap
    hints: List[ArticleHint] = []
    errors: List[str] = []
    adapter = PoliteAdapter()
    cache = DiscoveryCache(cache_rows or {}, since)
    session_html = requests.Session()
    session_html.headers.update(HTML_HEADERS)
    sm = requests.Session()
    sm.headers.update(SITEMAP_HEADERS)
    for s in (session_html, sm):
        s.mount("https://", adapter)
        s.mount("http://", adapter)

    def report() -> None:
        if http is not None:
            http.update(requests=adapter.requests, not_modified=adapter.not_modified,
                        replayed=cache.replayed, bytes=adapter.bytes,
                        cache_updates=cache.updates)

    try:
        origin = pick_accessible_origin(session_html, entry["url"])
        session_html.headers["Referer"] = origin + "/"
    except Exception as exc:
        # Nothing can run without an origin, so this one is genuinely fatal.
        report()
        return [], f"origin probe failed: {type(exc).__name__}: {exc}"

    # Each method runs independently. A failing sitemap must not cost us the feed:
    # the methods are alternative routes to the same site, not a pipeline. A
    # throttled host is the exception - nothing more is sent to it this pass.
    if entry.get("sitemap") and not adapter.tripped:
        try:
            hints += collect_from_sitemaps(sm, entry["url"], since, end,
                                           max_per_source=max_per_source, report=truncation,
                                           cache=cache)
            if truncation:
                logger.warning("[collect] %s: sitemap truncated: %s",
                               slug_for(entry), truncation["note"])
        except Exception as exc:
            errors.append(f"sitemap: {type(exc).__name__}: {exc}")

    if entry.get("feeds") and not adapter.tripped:
        try:
            feed_hints = collect_from_feeds(session_html, origin, cache=cache)
            # Feeds are not window-limited by the collector, so filter here.
            hints += [h for h in feed_hints
                      if h.published_at is None or in_range(h.published_at, since, end)]
        except Exception as exc:
            errors.append(f"feeds: {type(exc).__name__}: {exc}")

    if entry.get("frontpage") and not adapter.tripped:
        try:
            hints += collect_from_frontpage(session_html, origin,
                                            start_date=start, cap=300)
        except Exception as exc:
            errors.append(f"frontpage: {type(exc).__name__}: {exc}")

    if adapter.tripped:
        errors.append(f"throttled: {adapter.tripped}")
    report()

    unique = _dedupe_hints([on_configured_host(hint, entry) for hint in hints])
    kept = [hint for hint in unique
            if not url_is_excluded(hint.url, entry, hint.title)]
    if len(kept) < len(unique):
        logger.info("[collect] %s: dropped %d URL(s) by source exclusion policy",
                    slug_for(entry), len(unique) - len(kept))
    return kept, "; ".join(errors) if errors else None


def store_hints(conn: sqlite3.Connection, run_id: int, slug: str,
                hints: List[ArticleHint], source_kind: str = "news", *,
                full_text: bool = False) -> int:
    """Insert hints as raw items. Returns the number of rows written.

    For title-only sources, a retitled article is written as a further version
    rather than dropped, which is what mvp_plan means by "versioned raw source
    items"; a re-dated one is not, and nothing is written over a fetched body (see
    _is_retitle). Earlier versions are left intact. Full-text sources instead queue
    a body recheck and let the body stage decide whether content has changed.
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
            # Recorded at discovery so title_only rows carry it too. A body fetch
            # overwrites it with "page" when the article states its own date.
            "published_at_source": h.date_source if published else None,
        }

        # Every stored version is compared, not just the newest. Two
        # representations of one article that alternate, or a CMS that re-stamps
        # <lastmod> on a page already seen, each look "changed" relative to the
        # other - and comparing only the newest let that ratchet a fresh version
        # on every run, without bound.
        prior = conn.execute(
            "SELECT version, content_hash, title, "
            "json_extract(payload, '$.body_text') IS NOT NULL AS has_body "
            "FROM raw_item WHERE source_slug = ? AND external_id = ?",
            (slug, external_id),
        ).fetchall()
        if full_text:
            queue_body(conn, slug, external_id, payload_data)
            # Discovery metadata requests a body recheck. It must neither version
            # a CMS date change nor replace an enriched item with a headline stub.
            if prior:
                continue
            payload_data["body_status"] = "pending"
        elif prior and not _is_retitle(h, prior):
            continue
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
    entries = crawled_entries(sources.load_sources(str(path)))
    if not entries:
        raise ValueError(f"no sources in {path}")
    modes = {slug_for(entry): content_mode(entry) for entry in entries}

    end = datetime.now(tz=BERLIN_TZ)
    end_iso = end.isoformat()
    scope = f"collection:{kind}"
    plans = []

    # Give every source its own durable starting position. Initial positions are
    # written before network work so a source that fails on its first run retries
    # the exact same window rather than losing a little history every day.
    with session(db_path) as conn:
        global_mark = get_watermark(conn, scope)
        for entry in entries:
            slug = slug_for(entry)
            source_scope = source_watermark_scope(kind, slug)
            mark = get_watermark(conn, source_scope)
            if mark is None:
                mark = _legacy_source_start(conn, kind, slug, global_mark)
                if mark is None:
                    mark = (end - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
                set_watermark(conn, source_scope, mark)

            checkpoint = datetime.fromisoformat(mark)
            start = end - timedelta(days=days) if days is not None else checkpoint
            # An explicit --days window beginning after this source's checkpoint
            # leaves a hole. Crawl it, but do not claim the missing interval.
            leaves_gap = start > checkpoint
            if leaves_gap:
                logger.warning(
                    "[collect] %s window starts %s but watermark is %s - %s of "
                    "history would be skipped, so its watermark will not advance",
                    slug, start.isoformat(), mark, start - checkpoint)
            plans.append({"entry": entry, "slug": slug, "start": start,
                          "scope": source_scope, "leaves_gap": leaves_gap,
                          "cache_rows": load_discovery_cache(conn, slug)})

        earliest = min(plan["start"] for plan in plans)
        run_id = start_run(conn, kind, earliest.isoformat(), end_iso)

    logger.info("[collect] %s: %d sources, earliest window %s -> %s",
                kind, len(entries), earliest.isoformat(), end_iso)

    def work(plan):
        entry = plan["entry"]
        truncation: Dict[str, Any] = {}
        http: Dict[str, Any] = {}
        try:
            hints, error = collect_source(entry, plan["start"], end, max_per_source,
                                          truncation=truncation,
                                          cache_rows=plan["cache_rows"], http=http)
        except Exception as exc:  # noqa: BLE001 - one source must not end the run
            hints, error = [], f"{type(exc).__name__}: {exc}"
        return plan, hints, error, truncation, http

    summary = {"kind": kind, "sources": len(entries), "ok": 0, "zero": 0,
               "failed": 0, "found": 0, "stored": 0,
               "start": earliest.isoformat(), "end": end_iso,
               "per_source": [], "run_id": run_id}

    # Commit each completed source immediately. Like the body queue, a stopped
    # process keeps completed work and the unfinished sources retain their marks.
    with ThreadPoolExecutor(max_workers=workers or CRAWLER_WORKERS) as ex:
        futures = [ex.submit(work, plan) for plan in plans]
        for future in as_completed(futures):
            plan, hints, error, truncation, http = future.result()
            entry, slug = plan["entry"], plan["slug"]
            if http:
                logger.info("[collect] %s: %d request(s), %d not modified, %d file(s) "
                            "replayed, %.2f MB", slug, http["requests"],
                            http["not_modified"], http["replayed"], http["bytes"] / 1e6)
            # A truncated traversal stays ok; the note rides in the error column,
            # prefixed so readers can tell it from a failure without a migration.
            note = error or (f"truncated: {truncation['note']}" if truncation else None)
            with session(db_path) as conn:
                # Store whatever was collected even when a method failed. Discarding
                # a successful sitemap sweep because its feed timed out loses data.
                stored = store_hints(conn, run_id, slug, hints, source_kind=kind,
                                     full_text=modes[slug] == "full_text")
                # Saved whatever the source's status: a row holds what its file
                # yielded, so replaying it is right even after a failed pass.
                save_discovery_cache(conn, slug, http.get("cache_updates") or {})
                if error:
                    status = "failed"
                    summary["failed"] += 1
                    logger.warning("[collect] %s FAILED (%d hints kept): %s",
                                   slug, len(hints), error)
                else:
                    status = "ok" if hints else "zero"
                    summary["ok" if hints else "zero"] += 1
                    if not plan["leaves_gap"]:
                        advance_watermark(conn, plan["scope"], end_iso)
                summary["found"] += len(hints)
                summary["stored"] += stored
                summary["per_source"].append(
                    {"slug": slug, "organization": entry.get("organization"),
                     "status": status, "found": len(hints), "stored": stored,
                     "start": plan["start"].isoformat(),
                     "watermark_advanced": not error and not plan["leaves_gap"],
                     "error": note,
                     "http": {k: http[k] for k in ("requests", "not_modified",
                                                   "replayed", "bytes") if k in http}})
                record_source_result(conn, run_id, slug, status,
                                     items_found=len(hints), items_stored=stored,
                                     error=note)

    gaps = sum(1 for plan in plans if plan["leaves_gap"])
    with session(db_path) as conn:
        # Keep the former aggregate checkpoint for status reporting, old code, and
        # migration of sources that existed before per-source checkpoints.
        if summary["failed"] == 0 and gaps == 0:
            advance_watermark(conn, scope, end_iso)
        elif summary["failed"]:
            logger.warning("[collect] %d source(s) failed - aggregate watermark "
                           "not advanced", summary["failed"])
        summary["watermark_advanced"] = summary["failed"] == 0 and gaps == 0
        finish_run(conn, run_id, "ok" if summary["failed"] == 0 else "failed",
                   note=f"{summary['stored']} new items")

    # Discovery is committed before any page fetch. The durable body queue is
    # independent of the discovery watermark, including for feed items that vanish.
    if "full_text" in modes.values():
        summary["bodies"] = run_body_fetch(path, kind=kind, limit=limit, db_path=db_path)
    return summary
