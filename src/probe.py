"""Single-site crawl probe used to tune a source's JSON entry.

Reports which discovery methods a domain actually supports and how its article URLs
are distributed across path prefixes, so `sitemap` / `feeds` / `frontpage` and
`allowed_dirs` can be set from evidence instead of guesswork.

Probing a domain that is absent from the sources file runs every discovery method
with no directory filter, because SourceLoader.get_site_rules returns None for an
unknown domain and the crawler then allows each feature. Passing --sources loads a
file and re-runs the same probe under the rules already written for that domain,
which is how a drafted entry gets verified.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
import tldextract

from src.collect import is_furniture, is_malformed, url_is_excluded
from src.logger import get_logger
from src.polite_http import PoliteAdapter
from vendor.newscrawler.crawler import (
    BERLIN_TZ,
    COMMON_FEED_PATHS,
    HTML_HEADERS,
    SITEMAP_HEADERS,
    ArticleHint,
    collect_from_feeds,
    collect_from_frontpage,
    collect_from_sitemaps,
    domain_of,
    in_range,
    normalize_url,
    pick_accessible_origin,
    url_matches_dirs,
)
from vendor.newscrawler.crawler_html_utils import fetch_html
from vendor.newscrawler.source_loader import sources

logger = get_logger(__name__)

# An empty answer is often a throttle or a cold cache rather than an absent feature,
# and writing "feeds": false for a site that has feeds is a silent loss of coverage.
# The probe retries all three methods, because its whole purpose is deciding
# whether a method works.
ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 3.0


@dataclass
class MethodResult:
    """Outcome of one discovery method against one site."""

    name: str
    hints: List[ArticleHint] = field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 1
    cap: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.hints)

    @property
    def capped(self) -> bool:
        """True when the collector stopped at its limit, so counts are a floor."""
        return self.cap is not None and len(self.hints) >= self.cap

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        return "ok" if self.hints else "empty"


class FileRecorder:
    """Records what each sitemap file gave, by duck-typing the discovery cache.

    ``collect_from_sitemaps`` already offers every fetched file to an optional
    ``cache`` (``src/polite_http.DiscoveryCache``), which is exactly the per-file
    hook the probe needs, so nothing in the crawler has to change to report this.
    ``headers`` returns nothing: a probe wants the real file, never a 304.
    """

    def __init__(self) -> None:
        self.files: Dict[str, Dict] = {}
        self.parents: Dict[str, str] = {}

    def _row(self, url: str) -> Dict:
        return self.files.setdefault(url, {"entries": [], "validators": (None, None)})

    def parent_of(self, url: str) -> Optional[str]:
        """The index that listed this file, which a {LATEST} pin has to name."""
        return self.parents.get(url)

    def headers(self, url: str) -> Dict[str, str]:
        return {}

    def replay(self, url: str):  # pragma: no cover - headers() never asks for one
        raise AssertionError("the probe sends no conditional requests")

    def seen(self, url: str, response) -> None:
        self._row(url)["validators"] = (response.headers.get("ETag"),
                                        response.headers.get("Last-Modified"))

    def remember(self, url: str, entries, children) -> None:
        self._row(url)["entries"] = entries
        for child, _lastmod in children:
            self.parents.setdefault(child, url)


def file_yield(recorder: "FileRecorder", entry: Dict, start: datetime,
               end: datetime) -> Dict[str, Dict]:
    """Per sitemap file: what it would actually contribute to the corpus.

    Counts a URL only if it survives everything a real collection applies -
    the window, ``allowed_dirs``, the source's exclusion rules, furniture and
    malformed URLs - because a file holding 4,000 archive URLs contributes
    nothing and should read as nothing.
    """
    allowed_dirs = entry.get("allowed_dirs") or None
    out: Dict[str, Dict] = {}
    for url, row in recorder.files.items():
        kept, news_dates, titled = set(), 0, 0
        for loc, when, title, date_source in row["entries"]:
            if when is None or not in_range(when, start, end):
                continue
            if not url_matches_dirs(loc, allowed_dirs):
                continue
            if url_is_excluded(loc, entry, title) or is_furniture(loc) or is_malformed(loc):
                continue
            kept.add(normalize_url(loc))
            news_dates += date_source == "news_sitemap"
            titled += bool(title)
        out[url] = {"entries": len(row["entries"]), "kept": kept,
                    "news_dates": news_dates, "titled": titled,
                    "validators": any(row["validators"]),
                    "parent": recorder.parents.get(url)}
    return out


def pin_suggestion(per_file: Dict[str, Dict]) -> Tuple[List[str], Set[str]]:
    """Choose the fewest files that cover every kept URL, news sitemaps first.

    Greedy set cover answers "which files carry the articles", but coverage alone
    is the wrong objective on its own: welt.de's monthly sitemap holds 1,290
    in-window URLs to its news sitemap's 815, so pure coverage picks the monthly
    file and dates every article by ``<lastmod>`` - the field CLAUDE.md says may
    never be printed to a customer. A file carrying ``<news:publication_date>`` is
    taken first whenever it still adds anything, and the plain files then cover
    what is left.

    Returns (chosen files, URLs no file covers - empty unless a file was unreadable).
    """
    remaining = set().union(*(f["kept"] for f in per_file.values())) if per_file else set()
    chosen: List[str] = []
    while remaining:
        gains = [(url, per_file[url]["kept"] & remaining) for url in per_file]
        gains = [(url, gain) for url, gain in gains if gain]
        if not gains:
            break
        best, gain = max(gains, key=lambda item: (per_file[item[0]]["news_dates"] > 0,
                                                  len(item[1])))
        chosen.append(best)
        remaining -= gain
    return chosen, remaining


# A date inside a sitemap URL, anchored on a four-digit year followed by a real
# month. Matching "09" or "16" on their own would template an id or a section
# number and suggest a pin that fetches nothing.
_URL_DATE = re.compile(r"(?P<y>20\d{2})(?P<s1>[-_/]?)(?P<m>0[1-9]|1[0-2])"
                       r"(?:(?P<s2>[-_/])(?P<d>[0-3]\d))?")


def token_hints(chosen: List[str], per_file: Dict[str, Dict], end: datetime) -> List[str]:
    """Say which chosen files carry a date or a page number that will roll.

    A pin naming this month or this week's page number is a pin that breaks at the
    next boundary, which is the one failure mode pinning adds; both have a token
    that survives it, and spiegel.de needs both at once.
    """
    hints: List[str] = []
    for url in chosen:
        pinned, notes = url, []

        match = _URL_DATE.search(url)
        if match and match.group("y") == f"{end:%Y}":
            template = "{YYYY}" + (match.group("s1") or "") + "{MM}"
            if match.group("d"):
                template += match.group("s2") + "{DD}"
            pinned = url[:match.start()] + template + url[match.end():]
            notes.append("names this month")

        # A number shared with siblings of the same shape is a page number.
        shape = re.sub(r"\d+", "{N}", url)
        siblings = [u for u in per_file if u != url and re.sub(r"\d+", "{N}", u) == shape]
        if siblings:
            rolled = re.sub(r"(?<=[/=_-])[0-9]+(?=[./]|$)", "{LATEST}", pinned)
            if rolled != pinned:
                pinned = rolled
                notes.append(f"{len(siblings)} sibling(s) of the same shape")

        if not notes:
            continue
        index = (per_file.get(url) or {}).get("parent") or "<the index listing them>"
        spec = (f'{{"url": "{pinned}", "index": "{index}"}}'
                if "{LATEST}" in pinned else f'"{pinned}"')
        hints.append(f"{url}\n    -> {'; '.join(notes)}; pin as {spec}")
    return hints


def _dir_key(url: str, depth: int) -> str:
    """Return the leading `depth` path segments of a URL, e.g. 'news/inland'."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return "(root)"
    return "/".join(segments[:depth])


def _looks_like_article(url: str) -> bool:
    """Rough check that a URL is an article rather than a section index.

    Used only to keep the directory table readable; the crawler itself does not
    filter this way. Section indexes are segment-poor, articles carry a slug.
    """
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return False
    last = segments[-1]
    has_slug = "-" in last or last.endswith((".html", ".htm"))
    if len(segments) == 1:
        # Flat permalinks (/some-article-title, common on WordPress) carry no
        # section prefix, so demand a longer slug to avoid counting section
        # pages such as /e-commerce.
        return has_slug and len(last) > 20
    return has_slug


def _window_counts(hints: List[ArticleHint], start: datetime, end: datetime) -> Tuple[int, int]:
    """Return (dated hints inside the window, hints carrying no date at all)."""
    in_window = sum(1 for h in hints if h.published_at and in_range(h.published_at, start, end))
    undated = sum(1 for h in hints if not h.published_at)
    return in_window, undated


def _run_method(name: str, collect: Callable[[], List[ArticleHint]], cap: Optional[int] = None) -> MethodResult:
    """Run one collector, retrying an empty result but never a raised one.

    An exception is a definite answer about this attempt and is reported as-is; an
    empty list is ambiguous, so it is retried before being believed.
    """
    result = MethodResult(name, cap=cap)
    for attempt in range(1, ATTEMPTS + 1):
        result.attempts = attempt
        try:
            result.hints = collect()
        except Exception as exc:  # noqa: BLE001 - one failing method must not end the probe
            result.error = f"{type(exc).__name__}: {exc}"
            break
        if result.hints:
            break
        if attempt < ATTEMPTS:
            logger.info("[probe] %s returned nothing, retrying (%d/%d)", name, attempt + 1, ATTEMPTS)
            time.sleep(RETRY_PAUSE_SECONDS)
    return result


def declared_feeds(session: requests.Session, origin: str) -> List[str]:
    """Return feed URLs the homepage advertises via <link rel="alternate">.

    collect_from_feeds only tries COMMON_FEED_PATHS, so a site publishing feeds at a
    path outside that list reads as having none. Surfacing what the page declares
    keeps that distinction visible instead of silently recording "feeds": false.
    """
    try:
        html = fetch_html(session, origin, timeout=(5, 8), use_playwright_fallback=False)
        if not html:
            return []
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
    except Exception as exc:  # noqa: BLE001 - an advisory check must never fail the probe
        logger.info("[probe] could not read declared feeds: %s", exc)
        return []

    found: List[str] = []
    for link in tree.xpath("//link[@rel='alternate'][@href]"):
        if "rss" in (link.get("type") or "").lower() or "atom" in (link.get("type") or "").lower():
            found.append(urljoin(origin + "/", link.get("href")))
    return list(dict.fromkeys(found))


def unreachable_feeds(feed_urls: List[str]) -> List[str]:
    """Return declared feeds that COMMON_FEED_PATHS would never try."""
    known = {p.rstrip("/") for p in COMMON_FEED_PATHS}
    return [u for u in feed_urls if urlparse(u).path.rstrip("/") not in known]


ALL_METHODS = ("sitemap", "feeds", "frontpage")


def probe_site(
    site_url: str,
    *,
    days: float = 7.0,
    max_per_source: int = 2000,
    frontpage_cap: int = 300,
    sources_path: Optional[str] = None,
    methods: Sequence[str] = ALL_METHODS,
) -> Dict:
    """Run the named discovery methods against one site and return a report.

    ``methods`` narrows the probe to what a question needs. Verifying a configured
    source's sitemap files should not also scrape its homepage: the probe is the
    heaviest thing we point at a publisher. For the same reason both sessions carry ``PoliteAdapter``,
    so a host answering 429 or 503 twice ends its probe instead of being walked.
    """
    unknown = [m for m in methods if m not in ALL_METHODS]
    if unknown:
        raise ValueError(f"unknown probe method(s): {', '.join(unknown)}")
    if not site_url.startswith("http"):
        site_url = "https://" + site_url

    mode = "verify"
    entry: Dict = {}
    if sources_path:
        loaded = sources.load_sources(sources_path)
        logger.info("[probe] loaded %d sources from %s", len(loaded), sources_path)
        # get_site_rules returns only the crawl switches; the exclusion rules that
        # decide what a file really contributes live on the entry itself.
        entry = next((e for e in loaded
                      if domain_of(e.get("url", "")) == domain_of(site_url)), {})
        if sources.get_site_rules(site_url) is None:
            logger.warning(
                "[probe] %s is not in %s - probing it as an unknown domain",
                domain_of(site_url), sources_path,
            )
            mode = "discovery"
    else:
        mode = "discovery"

    end = datetime.now(tz=BERLIN_TZ)
    start = end - timedelta(days=days)

    adapter = PoliteAdapter()
    session = requests.Session()
    session.headers.update(HTML_HEADERS)
    sitemap_session = requests.Session()
    sitemap_session.headers.update(SITEMAP_HEADERS)
    for each in (session, sitemap_session):
        each.mount("https://", adapter)
        each.mount("http://", adapter)

    origin = pick_accessible_origin(session, site_url)
    session.headers["Referer"] = origin + "/"

    recorder = FileRecorder()
    runners = {
        # Sitemap hints arrive already window- and dir-filtered by the crawler.
        "sitemap": lambda: _run_method("sitemap", lambda: collect_from_sitemaps(
            sitemap_session, site_url, start, end, max_per_source=max_per_source,
            cache=recorder
        ), cap=max_per_source),
        # Feeds return whatever the feed holds; the window filter is applied on report.
        "feeds": lambda: _run_method("feeds", lambda: collect_from_feeds(session, origin)),
        # Frontpage hints carry only a date read near the link, often none at all.
        "frontpage": lambda: _run_method("frontpage", lambda: collect_from_frontpage(
            session, origin, start_date=start, cap=frontpage_cap
        ), cap=frontpage_cap),
    }
    results = [runners[name]() for name in ALL_METHODS if name in methods]

    return {
        "site_url": site_url,
        "origin": origin,
        "declared_feeds": declared_feeds(session, origin) if "feeds" in methods else [],
        "domain": domain_of(site_url),
        "mode": mode,
        "start": start,
        "end": end,
        "days": days,
        "results": results,
        "methods": list(methods),
        "requests": adapter.requests,
        "bytes": adapter.bytes,
        "throttled": adapter.tripped,
        "entry": entry,
        "per_file": file_yield(recorder, entry, start, end),
    }


def directory_table(results: List[MethodResult], depth: int) -> Tuple[List[str], Dict[str, Counter]]:
    """Count article-shaped URLs per path prefix, per discovery method."""
    per_dir: Dict[str, Counter] = {}
    seen: Set[Tuple[str, str]] = set()
    for result in results:
        for hint in result.hints:
            url = normalize_url(hint.url)
            key = (result.name, url)
            if key in seen or not _looks_like_article(url):
                continue
            seen.add(key)
            per_dir.setdefault(_dir_key(url, depth), Counter())[result.name] += 1
    order = sorted(per_dir, key=lambda d: -sum(per_dir[d].values()))
    return order, per_dir


def suggest_dirs(order: List[str], per_dir: Dict[str, Counter], coverage: float = 0.9) -> List[str]:
    """Return the smallest prefix set covering `coverage` of article URLs, max 10.

    A starting point for review, not a decision: the printed table is what the
    directory choice should actually rest on.
    """
    # A prefix that groups only one article is not a section, it is that article's
    # slug. Sites with flat permalinks produce nothing but those, and suggesting
    # them as allowed_dirs would pin the crawl to the handful of URLs seen today.
    totals = [(d, sum(per_dir[d].values())) for d in order
              if d != "(root)" and sum(per_dir[d].values()) >= 2]
    grand = sum(count for _, count in totals)
    if not grand:
        return []
    kept: List[str] = []
    running = 0
    for directory, count in totals:
        kept.append(directory)
        running += count
        if running / grand >= coverage or len(kept) >= 10:
            break
    return kept


def draft_entry(report: Dict, allowed_dirs: List[str]) -> Dict:
    """Build the JSON row a source list would carry for this site."""
    results = {r.name: r for r in report["results"]}
    # domain_of keeps the host, so take the registered name: www.zeit.de -> Zeit.
    organization = tldextract.extract(report["origin"]).domain.replace("-", " ").title()
    return {
        "country": "Germany",
        "organization": organization,
        "type": "媒体",
        "language": "德语",
        "url": report["origin"].rstrip("/") + "/",
        "sitemap": results["sitemap"].ok,
        "feeds": results["feeds"].ok,
        # Frontpage scraping is the fallback, not an addition, where a sitemap works.
        "frontpage": results["frontpage"].ok and not results["sitemap"].ok,
        "allowed_dirs": allowed_dirs,
        "notes": "",
    }


def format_report(report: Dict, depth: int = 1, samples: int = 2) -> str:
    """Render a probe report as the text block printed to the console."""
    results: List[MethodResult] = report["results"]
    start, end = report["start"], report["end"]
    lines: List[str] = []

    lines.append(f"Site:   {report['site_url']}")
    if report["origin"].rstrip("/") != report["site_url"].rstrip("/"):
        lines.append(f"Origin: {report['origin']}  (redirected)")
    lines.append(f"Window: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}  ({report['days']:g}d)")
    if report["mode"] == "discovery":
        lines.append("Mode:   discovery - every method tried, no directory filter")
    else:
        lines.append("Mode:   verify - this domain's rules from the sources file are applied")
    lines.append("")

    lines.append(f"{'method':<12}{'status':<9}{'urls':>7}{'in window':>12}{'undated':>10}"
                 f"{'titled':>8}")
    lines.append("-" * 58)
    capped = []
    for result in results:
        in_window, undated = _window_counts(result.hints, start, end)
        notes = []
        if result.attempts > 1:
            notes.append(
                f"empty after {result.attempts} tries" if result.status == "empty"
                else f"succeeded on try {result.attempts}"
            )
        if result.capped:
            notes.append(f"hit cap {result.cap}")
            capped.append(result.name)
        note = f"  ({', '.join(notes)})" if notes else ""
        lines.append(
            f"{result.name:<12}{result.status:<9}{len(result.hints):>7}"
            f"{in_window:>12}{undated:>10}{sum(1 for h in result.hints if h.title):>8}{note}"
        )
        if result.error:
            lines.append(f"    -> {result.error}")
    lines.append("")
    lines.append("Undated frontpage URLs are expected; a real crawl dates them separately.")

    # collect_from_feeds autodiscovers declared feeds, so a non-standard path is no
    # longer a reason feeds came back empty. Only say something when the homepage
    # advertises feeds and we still got nothing - that is a real anomaly.
    declared = report.get("declared_feeds", [])
    feeds_result = next((r for r in results if r.name == "feeds"), None)
    if declared and feeds_result is not None and not feeds_result.ok:
        lines.append("")
        lines.append(
            f"This site advertises {len(declared)} feed(s) but none yielded articles. "
            "Autodiscovery reached them, so this is the feed itself being empty, "
            "gated, or malformed - worth opening by hand before recording feeds=false:"
        )
        for url in declared[:5]:
            lines.append(f"  {url}")
        if len(declared) > 5:
            lines.append(f"  ... and {len(declared) - 5} more")
    elif declared and unreachable_feeds(declared):
        lines.append("")
        lines.append(
            f"Feeds came from homepage autodiscovery ({len(declared)} advertised); "
            "the fixed COMMON_FEED_PATHS list alone would have missed them."
        )

    if capped:
        lines.append(
            f"{' and '.join(capped)} stopped at the cap, so the counts below are a floor "
            "and the mix may be skewed. Re-run with a larger --max/--frontpage-cap, or a "
            "shorter --days, before trusting the proportions."
        )
    lines.append("")

    order, per_dir = directory_table(results, depth)
    if not order:
        lines.append("No article-shaped URLs found. Nothing to base allowed_dirs on.")
        return "\n".join(lines)

    method_names = [r.name for r in results]
    lines.append(f"--- article URLs by path prefix (depth {depth}) ---")
    header = f"{'prefix':<34}" + "".join(f"{name:>11}" for name in method_names) + f"{'total':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for directory in order[:25]:
        counts = per_dir[directory]
        row = f"{'/' + directory:<34}"
        row += "".join(f"{counts.get(name, 0):>11}" for name in method_names)
        row += f"{sum(counts.values()):>8}"
        lines.append(row)
    if len(order) > 25:
        lines.append(f"... and {len(order) - 25} more prefixes")
    lines.append("")

    if samples:
        lines.append("--- sample URLs ---")
        for directory in order[:5]:
            shown = 0
            for result in results:
                for hint in result.hints:
                    url = normalize_url(hint.url)
                    if _looks_like_article(url) and _dir_key(url, depth) == directory:
                        lines.append(f"  {url}" + (f"\n      {hint.title}" if hint.title else ""))
                        shown += 1
                        if shown >= samples:
                            break
                if shown >= samples:
                    break
        lines.append("")

    lines.extend(sitemap_file_section(report))

    allowed = suggest_dirs(order, per_dir)
    if len(report.get("methods") or ALL_METHODS) < len(ALL_METHODS):
        lines.append(f"Only {', '.join(report['methods'])} probed, so no draft entry: "
                     f"its booleans would report an unprobed method as unsupported.")
        return "\n".join(lines)
    lines.append("--- draft entry (review before use) ---")
    lines.append(json.dumps(draft_entry(report, allowed), indent=2, ensure_ascii=False))
    lines.append("")
    lines.append("Trim allowed_dirs to the sections worth monitoring, then re-run with")
    lines.append("--sources <file> to confirm the entry behaves as intended.")
    return "\n".join(lines)


def sitemap_file_section(report: Dict) -> List[str]:
    """Per-file yield and the sitemap_urls block to paste.

    Collection runs nine times a day; every file listed here with nothing in the
    window is read on every one of those passes for nothing. ``validators`` says
    whether the server sends an ETag or Last-Modified: without them a conditional
    request cannot help either, so those rows are the expensive ones.
    """
    per_file: Dict[str, Dict] = report.get("per_file") or {}
    if not per_file:
        return []
    lines = ["--- sitemap files read (what each contributed) ---"]
    chosen, _uncovered = pin_suggestion(per_file)
    order = sorted(per_file, key=lambda u: (-len(per_file[u]["kept"]), u))
    others = {u: per_file[u]["kept"] for u in per_file}
    header = (f"{'kept':>6}{'unique':>8}{'entries':>9}{'news':>7}{'titled':>8}"
              f"{'valid':>7}  file")
    lines.append(header)
    lines.append("-" * min(len(header) + 40, 110))
    empty = 0
    for url in order[:40]:
        row = per_file[url]
        if not row["kept"]:
            empty += 1
            continue
        unique = row["kept"] - set().union(*(v for u, v in others.items() if u != url)) \
            if len(others) > 1 else row["kept"]
        mark = "*" if url in chosen else " "
        lines.append(f"{len(row['kept']):>6}{len(unique):>8}{row['entries']:>9}"
                     f"{row['news_dates']:>7}{row['titled']:>8}"
                     f"{'yes' if row['validators'] else 'no':>7} {mark}{url}")
    if empty:
        lines.append(f"{empty} further file(s) contributed nothing in this window "
                     f"and are read on every pass for nothing.")
    lines.append("")
    if chosen:
        lines.append("--- suggested sitemap_urls (verify before committing) ---")
        lines.append(json.dumps({"sitemap_urls": chosen}, indent=2, ensure_ascii=False))
        covered = len(set().union(*(per_file[u]["kept"] for u in chosen)))
        total = len(set().union(*(f["kept"] for f in per_file.values())))
        lines.append(f"{len(chosen)} of {len(per_file)} file(s) cover {covered}/{total} "
                     f"of this window's URLs.")
        for hint in token_hints(chosen, per_file, report["end"]):
            lines.append(f"  {hint}")
        lines.append("")
    return lines
