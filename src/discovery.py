"""Pinned sitemap discovery: fetch the files a source is known to publish.

Collection runs nine times a day. Walking a publisher's sitemap index on every
pass re-reads an archive that has not changed since the pass before: measured over
a live 50-hour window on 2026-09-16, faz.net answered 100 files of which 95 held
nothing in the window, and dvz.de 88 files of which 87 held nothing - while
``news-sitemap.xml`` alone held all 22 of its articles and cost 0.01 MB. Neither
host sends ``ETag`` or ``Last-Modified``, so the conditional requests in
``src/polite_http.py`` cannot help them either; they answer every request in full.

So a source may pin the files that carry its articles, and this module fetches
exactly those. There is no ``robots.txt`` read, no path guessing, no index walk and
no cap: a pinned list is three files, not an unknown tree, and every heuristic in
``collect_from_sitemaps`` - news-sitemaps first, newest child first, pruning an
archive by the date in its filename, ``max_per_source`` - exists to survive a walk
this path does not make. That function is not replaced and not modified. It stays
the engine behind ``run.py probe`` and ``tools/rediscover.py``, which is where
discovery now happens: once, deliberately, by a person reading the result.

The whole risk of pinning is that a source stops yielding without anything
failing, so the rule is that **an unreadable pin stops the source** - a 404, a
redirect to another host, markup where XML was promised - which holds the
watermark and reaches the health check. An *empty* pin does not: a news sitemap is
legitimately empty on a Sunday, and a rule that failed on that would fire about a
hundred times a year until nobody read the mail. The discriminator is whether the
file could be read, never whether it was full. What this cannot see - a file that
still parses but has quietly stopped carrying a section - is what the independent
canaries in ``health/canary.py`` and ``tools/rediscover.py`` are for.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from src.logger import get_logger
from vendor.newscrawler.crawler_html_utils import HostThrottled
from vendor.newscrawler.crawler import (ArticleHint, fetch_sitemap_urls,
                                        normalize_url, url_matches_dirs)

logger = get_logger(__name__)

DATE_TOKENS = ("{YYYY}", "{MM}", "{DD}")
LATEST_TOKEN = "{LATEST}"
PAGE_TOKEN = "{PAGE}"


class PinnedSitemapError(RuntimeError):
    """A pinned sitemap could not be read, so the source cannot be trusted quiet."""


def has_pins(entry: Dict[str, Any]) -> bool:
    """True when this source's sitemaps are pinned rather than discovered.

    Like ``feed_urls``, pinning only takes effect for a method the source enables:
    an entry carrying ``sitemap_urls`` without ``"sitemap": true`` is not crawled
    by sitemap at all, and this returns False for it.
    """
    return bool(entry.get("sitemap")) and bool(entry.get("sitemap_urls"))


def _specs(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise ``sitemap_urls`` into one dict per pin.

    A plain string is the usual case. The object form exists for ``{LATEST}``,
    which needs to be told which index lists the numbered children::

        {"url": ".../sitemap.xml?page={LATEST}", "index": ".../sitemap.xml"}
        {"url": ".../sitemap-{YYYY}-{MM}_{LATEST}.xml", "index": "...", "latest_count": 3}
        {"url": ".../sitemap-{YYYY}-{MM}_{PAGE}.xml", "pages": [1, 6]}
    """
    specs: List[Dict[str, Any]] = []
    for raw in entry.get("sitemap_urls") or []:
        spec = {"url": raw} if isinstance(raw, str) else dict(raw or {})
        url = spec.get("url")
        if not url:
            raise PinnedSitemapError(f"sitemap_urls entry without a url: {raw!r}")
        if LATEST_TOKEN in url and not spec.get("index"):
            raise PinnedSitemapError(
                f"{url}: {LATEST_TOKEN} needs an \"index\" naming the sitemap that "
                f"lists the numbered files")
        if PAGE_TOKEN in url:
            pages = spec.get("pages")
            if (not isinstance(pages, (list, tuple)) or len(pages) != 2
                    or not all(isinstance(n, int) for n in pages) or pages[0] > pages[1]
                    or pages[0] < 0):
                raise PinnedSitemapError(
                    f"{url}: {PAGE_TOKEN} needs \"pages\": [first, last]")
        if LATEST_TOKEN in url and PAGE_TOKEN in url:
            raise PinnedSitemapError(f"{url}: use {LATEST_TOKEN} or {PAGE_TOKEN}, not both")
        spec.setdefault("latest_count", 1)
        specs.append(spec)
    return specs


def _is_absent(exc: BaseException) -> bool:
    """True only for a definite "this file does not exist".

    The group tolerance below exists for one case: a page or a month that has not
    begun yet. That case answers 404. A 500, a timeout or a refused connection is
    not evidence of absence, and tolerating one would advance the watermark over a
    window the file was never read for - the silent loss pinning is meant to end.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (404, 410)


def _months(since: datetime, end: datetime) -> List[date]:
    out, year, month = [], since.year, since.month
    while (year, month) <= (end.year, end.month):
        out.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def expand_dates(url: str, since: datetime, end: datetime) -> List[str]:
    """Resolve ``{YYYY}``/``{MM}``/``{DD}`` to every file the window touches.

    One URL per period at the finest granularity the template names, which is what
    keeps a rolling filename pinnable without discovery. The window, not a special
    case, is what makes a month boundary work: with the 48-hour collection overlap
    a run on the 1st has ``since`` in the previous month, so both months resolve
    and nothing is lost at the boundary. Newest first, because that is the file
    most likely to hold the window's articles.
    """
    if not any(token in url for token in DATE_TOKENS):
        return [url]

    def fill(when: date) -> str:
        return (url.replace("{YYYY}", f"{when.year:04d}")
                   .replace("{MM}", f"{when.month:02d}")
                   .replace("{DD}", f"{when.day:02d}"))

    if "{DD}" in url:
        days = (end.date() - since.date()).days
        periods = [since.date() + timedelta(days=n) for n in range(days + 1)]
    elif "{MM}" in url:
        periods = _months(since, end)
    else:
        periods = [date(year, 1, 1) for year in range(since.year, end.year + 1)]
    # dict.fromkeys: a template naming only {YYYY} over a 13-month window would
    # otherwise resolve the same file twice.
    return list(dict.fromkeys(fill(when) for when in reversed(periods)))


def expand_pages(url: str, spec: Dict[str, Any]) -> List[str]:
    """Resolve ``{PAGE}`` over the configured range, first page first.

    ``{LATEST}`` asks an index which file is newest, which is right when the
    highest number is the newest - ohn.haendlerbund's page 34, Wettbewerbszentrale's
    post-sitemap4. Spiegel numbers the other way: within one month, page 1 holds the
    newest 50 articles and page 30 the start of the month, so the newest pages are a
    fixed low range and no index lookup is needed to name them. Asking its index
    instead would cost a 23,635-child file every pass and return four empty files.

    The range is one group (see ``collect_from_pinned``), so pages that do not exist
    yet in a month that has just begun are logged, not fatal.
    """
    if PAGE_TOKEN not in url:
        return [url]
    first, last = spec["pages"]
    return [url.replace(PAGE_TOKEN, str(n)) for n in range(first, last + 1)]


def resolve_latest(session: requests.Session, url: str, spec: Dict[str, Any],
                   cache: Any = None) -> List[str]:
    """Read the index once and return its highest-numbered matching children.

    Costs one extra request per pass and keeps ohn.haendlerbund, bevh, etailment
    and Wettbewerbszentrale pinned despite page numbers that roll. The index is
    fetched strictly: a source whose index has gone is not quietly yielding
    nothing.
    """
    index_url = spec["index"]
    _entries, children = fetch_sitemap_urls(session, index_url, cache, strict=True)
    # The token marks where the number is, and matching stops there: ohn.haendlerbund
    # and bevh serve TYPO3 paged sitemaps whose every page carries its own cHash
    # (?page=34&cHash=7ea2f388...), which no template can predict. What follows the
    # number is taken from the child's own URL, the only place it is knowable. When
    # the template's tail is a plain suffix it still has to match, so ".xml" does not
    # collect a sibling that is not one.
    head, _, tail = url.partition(LATEST_TOKEN)
    suffix = tail if tail and "?" not in tail and not any(c.isdigit() for c in tail) else None
    pattern = re.compile(re.escape(head) + r"(\d+)")
    numbered = []
    for child, _lastmod in children:
        match = pattern.match(child)
        if match and (suffix is None or child.endswith(suffix)):
            numbered.append((int(match.group(1)), child))
    if not numbered:
        raise PinnedSitemapError(
            f"{index_url}: lists no child matching {url} ({len(children)} children)")
    numbered.sort(reverse=True)
    count = max(1, int(spec.get("latest_count") or 1))
    return [child for _number, child in numbered[:count]]


def collect_from_pinned(entry: Dict[str, Any], since: datetime, end: datetime, *,
                        session: requests.Session, cache: Any = None) -> List[ArticleHint]:
    """Fetch this source's pinned sitemaps and return the hints inside the window.

    Raises ``PinnedSitemapError`` when a pin could not be read, which makes the
    source fail in ``collect_source`` so its watermark holds. A pin that expands to
    several URLs - a month boundary, a ``{PAGE}`` range - is judged as one unit, but
    only absence is forgiven: a 404 on one of them is the file for a month or a page
    that has not begun yet, while a 500, a timeout or markup fails the source even
    when a sibling read. Tolerating the second kind would advance the watermark over
    a window that was never read.

    Entries are not capped. The pinned set is small by construction and everything
    in it is filtered to ``[since, end]``; what a wide recovery window returns is
    what that window asked for.
    """
    allowed_dirs = entry.get("allowed_dirs") or None
    hints: List[ArticleHint] = []
    undated = 0

    for spec in _specs(entry):
        urls = expand_dates(spec["url"], since, end)
        if LATEST_TOKEN in spec["url"]:
            resolved: List[str] = []
            for url in urls:
                resolved += resolve_latest(session, url, spec, cache)
            urls = list(dict.fromkeys(resolved))
        elif PAGE_TOKEN in spec["url"]:
            urls = [page for url in urls for page in expand_pages(url, spec)]

        read, failures, unreadable = 0, [], []
        for url in urls:
            try:
                url_entries, _children = fetch_sitemap_urls(session, url, cache, strict=True)
            except (PinnedSitemapError, HostThrottled):
                # A throttled host must stop the source's pass, not be retried
                # against the next page of the same range.
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised below unless absent
                failures.append(f"{url}: {type(exc).__name__}: {exc}")
                if not _is_absent(exc):
                    unreadable.append(f"{url}: {type(exc).__name__}: {exc}")
                continue
            read += 1

            kept = []
            for loc, when, title, date_source in url_entries:
                if when is None:
                    # A pinned file has no containing index to inherit a date from,
                    # and an undated entry cannot be window-filtered at all.
                    undated += 1
                    continue
                kept.append((loc, when, title, date_source))
                if not (since <= when <= end):
                    continue
                if not url_matches_dirs(loc, allowed_dirs):
                    continue
                hints.append(ArticleHint(url=normalize_url(loc), published_at=when,
                                         title=title, source="sitemap",
                                         date_source=date_source))
            if cache is not None:
                cache.remember(url, kept, [])

        if not read or unreadable:
            raise PinnedSitemapError(
                "pinned sitemap unavailable: " + "; ".join(unreadable or failures))
        if failures:
            # Every failure here answered 404, and a sibling read: a page or month
            # that has not started yet, which is not this source being down.
            logger.info("[pinned] %s: %d of %d file(s) absent: %s",
                        entry.get("url"), len(failures), len(urls), "; ".join(failures))

    if undated:
        logger.info("[pinned] %s: %d entry/entries carried no date and were skipped",
                    entry.get("url"), undated)
    return hints
