"""Article bodies, immutable content versions, and a durable retry queue.

Sources explicitly configured with content_mode=full_text are fetched in bulk.
Title-only sources enter only through explicit keeps in a client's title-gate
JSONL; the queue retains that routing metadata so no later body keyword match is
required. No matching happens in this module.

Fetching is an escalation ladder, not a single request - see fetch_body. Each
rung costs more than the one below it, so each runs only when the cheaper rung
failed in the one way it can fix. The vendored crawler's own fetch_html is
deliberately not used as the entry point: it returns markup only, which discards
the status code, content type and final URL that decide whether an outcome is
unavailable (stop retrying) or failed (retry), and it tries a paywall login
before anything else. Its useful leaves - the browser and the paywall handler -
are called directly from the rungs that need them.

No paid fetch fallback (BrightData) runs here; no configured source needs it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from src.config import (BODY_FETCH_BROWSER, BODY_FETCH_DELAY, BODY_FETCH_LIMIT,
                        BODY_FETCH_MAX_ATTEMPTS, BODY_FETCH_TIMEOUT, PDF_MAX_PAGES,
                        ROOT)
from src.db import finish_run, record_source_result, session, start_run, utcnow
from src.logger import get_logger
from vendor.newscrawler.crawler import HTML_HEADERS, url_matches_dirs

logger = get_logger(__name__)

# The escalation ladder fires on the rung below having failed in one specific
# way, so those two diagnoses are constants rather than inline strings.
NO_ARTICLE_TEXT = "no usable article text (empty, short, or JS-only page)"
PAYWALL_DECLARED = "publisher declares a paywall"

# When this many connection failures run consecutively across this many distinct
# hosts without a single success, the pass stops: that is this machine's network,
# not that many publishers at once. interleave_by_source already round-robins the
# queue, so a streak spans different hosts by construction - the host count is
# the guard for the case where only one or two sources have pending work.
TRANSPORT_BREAKER_FAILURES = 10
TRANSPORT_BREAKER_HOSTS = 3


def interleave_by_source(tasks: list[Any]) -> list[Any]:
    """Round-robin tasks across their source, preserving order within each source.

    The incoming sort is (attempted_at, source_slug, external_id), which gets the
    retry priority right but makes the batch alphabetical by host: the first body
    run sent all 100 of its requests to one domain and touched nothing else. That
    matters twice over — a long unbroken burst at one publisher is impolite and
    invites a block, and a large backlog would otherwise be spent entirely on
    whichever source sorts first.
    """
    per_source: dict[str, deque] = defaultdict(deque)
    for task in tasks:
        per_source[task["source_slug"]].append(task)

    ordered: list[Any] = []
    queues = list(per_source.values())
    while queues:
        queues = [q for q in queues if q]
        for q in list(queues):
            ordered.append(q.popleft())
    return ordered


def pace_host(url: str, seen: dict[str, float], delay: float = BODY_FETCH_DELAY) -> float:
    """Sleep only as long as needed to keep `delay` between hits on one host.

    Per host rather than global: interleaving already spaces domains apart, so a
    blanket sleep would just make a mixed batch slower without making it politer.
    When one source is alone in the queue — the tail of a backlog — this still
    paces it properly. Returns the seconds actually slept, for tests.
    """
    if delay <= 0:
        return 0.0
    host = urlsplit(url).netloc.lower()
    now = time.monotonic()
    previous = seen.get(host)
    slept = 0.0
    if previous is not None:
        remaining = delay - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
            slept = remaining
    seen[host] = time.monotonic()
    return slept


def content_mode(entry: dict[str, Any]) -> str:
    mode = entry.get("content_mode", "title_only")
    if mode not in {"title_only", "full_text"}:
        raise ValueError(f"invalid content_mode {mode!r} for {entry['url']}")
    return mode


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return "\n\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def queue_body(conn: sqlite3.Connection, slug: str, external_id: str,
               payload: dict[str, Any], *, only_missing: bool = False,
               title_gate_route: dict[str, Any] | None = None) -> None:
    """A changed discovery hint requests a recheck, never a content version."""
    hint = {key: payload.get(key) for key in
            ("url", "title", "published_at", "discovered_via")}
    fingerprint = digest(hint)
    current = conn.execute(
        "SELECT discovery_hash, hint_payload FROM body_fetch "
        "WHERE source_slug=? AND external_id=?",
        (slug, external_id),
    ).fetchone()
    routes = {}
    if current:
        try:
            routes = json.loads(current["hint_payload"]).get("title_gate_routes", {})
        except (TypeError, ValueError):
            routes = {}
    if title_gate_route:
        client = title_gate_route.get("client")
        if not client:
            raise ValueError("a title-gate route needs a client")
        routes[client] = title_gate_route
    if routes:
        # Routing metadata is client-specific operational state, not shared source
        # content and not part of the discovery fingerprint. After a successful
        # fetch it is the title lane's durable handoff to the full assessor.
        hint["title_gate_routes"] = routes
    if current and (only_missing or current["discovery_hash"] == fingerprint):
        encoded = json.dumps(hint, ensure_ascii=False)
        if encoded != current["hint_payload"]:
            conn.execute(
                "UPDATE body_fetch SET hint_payload=? "
                "WHERE source_slug=? AND external_id=?",
                (encoded, slug, external_id),
            )
        return
    conn.execute(
        "INSERT INTO body_fetch (source_slug, external_id, discovery_hash, hint_payload) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(source_slug, external_id) DO UPDATE SET "
        "discovery_hash=excluded.discovery_hash, hint_payload=excluded.hint_payload, "
        "status='pending', error=NULL, attempts=0",
        (slug, external_id, fingerprint, json.dumps(hint, ensure_ascii=False)),
    )


@dataclass
class BodyResult:
    status: str
    text: str | None = None
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    published_at_raw: str | None = None
    error: str | None = None
    # Nothing answered at all - no socket, no name. That is this host's network
    # until something else in the same pass succeeds, so it must not spend the
    # URL's attempt budget. See the breaker in run_body_fetch.
    transport: bool = False


def _page_published_value(soup: BeautifulSoup) -> str | None:
    """Return the publisher's untrusted page-date string, if present.

    Structured attributes only: JSON-LD ``datePublished``, then
    ``article:published_time``, then a lone ``<time datetime>``. Visible text is
    never read - sampled first visible dates included a future seminar and a
    listing entry, and a confident wrong date is worse than none. Modification
    dates are left out on purpose; they are ``lastmod`` under another name.
    """
    def walk(value):
        if isinstance(value, dict):
            date = value.get("datePublished")
            if isinstance(date, str) and date.strip():
                return date.strip()
            return next(filter(None, (walk(v) for v in value.values())), None)
        if isinstance(value, list):
            return next(filter(None, (walk(v) for v in value)), None)
        return None

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            found = walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
        if found:
            return found
    meta = soup.find("meta", property="article:published_time")
    content = meta.get("content") if meta else None
    if content and content.strip():
        return content.strip()
    return _lone_time_datetime(soup)


def _lone_time_datetime(soup: BeautifulSoup) -> str | None:
    """The one day every ``<time datetime>`` on the page agrees on, or None.

    bevh and DSLV state their article date in a single <time> element and
    nowhere else. Several <time> elements naming different days are refused
    rather than guessed at: a Verbraucherzentrale class-action record carries
    filed, served and status dates, none of which is a publication date, and a
    listing page carries one per teaser. Elements whose datetime is not a date
    (durations such as PT5M, empty strings) do not count either way.
    """
    found: list[tuple[str, str]] = []
    for tag in soup.find_all("time"):
        raw = (tag.get("datetime") or "").strip()
        normalized = normalize_page_published_at(raw) if raw else None
        if normalized:
            found.append((raw, normalized[:10]))
    if not found or len({day for _raw, day in found}) != 1:
        return None
    return found[0][0]


_LOGISTIK_DATE = re.compile(
    r"^(Mo|Di|Mi|Do|Fr|Sa|So),\s*(\d{2})/(\d{2})/(\d{4})\s*-\s*"
    r"(\d{2}):(\d{2})$"
)
_GERMAN_WEEKDAY = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3,
                   "Fr": 4, "Sa": 5, "So": 6}


def normalize_page_published_at(value: str | None) -> str | None:
    """Validate a page date and return sortable ISO 8601, or reject it.

    LOGISTIK HEUTE emits a non-ISO Drupal value whose numeric portion is US
    month/day order despite the German weekday.  ``11/16`` proves that order.
    The weekday is checked as a guard against silently swapping ambiguous dates.
    """
    if not value or not value.strip():
        return None
    value = value.strip()
    if re.fullmatch(r"\d{10}", value):
        # DSLV writes epoch seconds into <time datetime>. Ten digits spans
        # 2001-2286, so nothing plausible is refused and nothing else matches.
        return datetime.fromtimestamp(int(value), tz=ZoneInfo("Europe/Berlin")).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass

    match = _LOGISTIK_DATE.fullmatch(value)
    if not match:
        return None
    weekday, month, day, year, hour, minute = match.groups()
    try:
        parsed = datetime(int(year), int(month), int(day), int(hour), int(minute),
                          tzinfo=ZoneInfo("Europe/Berlin"))
    except ValueError:
        return None
    if parsed.weekday() != _GERMAN_WEEKDAY[weekday]:
        return None
    return parsed.isoformat()


def _page_published_at(soup: BeautifulSoup) -> str | None:
    """The validated publication date the page states, or None.

    A sitemap's <lastmod> says only that a URL changed, and publishers rewrite it
    in bulk: BVL stamps all 5,323 of its URLs with one timestamp, and etailment's
    2026 migration restamped an archive back to 2001. Both make old pages look new.
    The page's own datePublished survives that, so it is what we keep.
    """
    return normalize_page_published_at(_page_published_value(soup))


def _remove_tag_clouds(root: BeautifulSoup) -> None:
    """Remove dense publisher tag clouds without deleting ordinary links.

    LOGISTIK HEUTE renders its global taxonomy as a paragraph of dozens of
    ``tagcloud-prio-*`` spans outside a semantic footer.  Trafilatura therefore
    treated it as article prose.  Requiring eight markers keeps this structural
    and avoids deleting a genuine paragraph that happens to contain one tag link.
    """
    candidates = []
    seen: set[int] = set()
    for marker in root.select('[class*="tagcloud-prio-"]'):
        container = marker.find_parent(("p", "ul", "ol")) or marker.find_parent("div")
        if container is not None and id(container) not in seen:
            seen.add(id(container))
            candidates.append(container)
    for container in candidates:
        if len(container.select('[class*="tagcloud-prio-"]')) >= 8:
            container.decompose()


def _declares_paywall(soup: BeautifulSoup) -> bool:
    def walk(value):
        if isinstance(value, dict):
            if value.get("isAccessibleForFree") in (False, "false", "False"):
                return True
            return any(walk(child) for child in value.values())
        if isinstance(value, list):
            return any(walk(child) for child in value)
        return False

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if walk(json.loads(script.string or script.get_text())):
                return True
        except (ValueError, TypeError):
            continue
    for tag in soup.select('[itemprop="isAccessibleForFree"]'):
        if str(tag.get("content", tag.get_text())).strip().lower() == "false":
            return True
    return False


_LOGIN_TITLE = re.compile(r"^\s*(login|anmelden|sign in|log in)\b", re.IGNORECASE)
_LOGIN_PHRASES = re.compile(
    r"login für abonnenten|sie sind noch kein abonnent|\bim probeabo\b|"
    r"\d+\s+wochen kostenlos testen|jetzt abonnent werden", re.IGNORECASE)


def _is_login_wall(title: str | None, text: str) -> bool:
    """True when the page served is a subscriber login rather than the article.

    The title is decisive on its own - no article on these sources is called
    "Login". The phrase test is deliberately gated on length, because a genuine
    article about subscription models may well mention a Probeabo, while a login
    wall is always short.
    """
    if title and _LOGIN_TITLE.match(title):
        return True
    return bool(_LOGIN_PHRASES.search(text)) and len(text.split()) < 400


def _extract_article(markup: bytes | str, final_url: str) -> BodyResult:
    """Turn fetched HTML into a BodyResult, whatever rung produced it.

    Kept separate from fetching so a rendered or authenticated page goes through
    exactly the same paywall, title and extraction rules as a plain one.
    """
    import trafilatura

    # Pass bytes through unchanged so HTML charset declarations are respected
    # even if the server omits a charset (requests otherwise defaults to Latin-1).
    soup = BeautifulSoup(markup, "lxml")
    if _declares_paywall(soup):
        return BodyResult("unavailable", url=final_url, error=PAYWALL_DECLARED)
    # Read before the tree is pruned below: the JSON-LD block sits outside
    # the article element that extraction narrows to.
    published_raw = _page_published_value(soup)
    published = normalize_page_published_at(published_raw)
    heading = soup.find("h1")
    og_title = soup.find("meta", property="og:title")
    title = (heading.get_text(" ", strip=True) if heading else
             og_title.get("content") if og_title else None)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    page_title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(marker in page_title for marker in
           ("just a moment", "access denied", "verify you are human", "captcha")):
        return BodyResult("failed", url=final_url, error="access challenge page")
    articles = soup.find_all("article")
    # A lone <article> is the publisher stating the body boundary itself, which is
    # better than any heuristic. Several of these mean the page also carries teasers,
    # so there is no boundary to trust and the whole document is handed over instead.
    bounded = len(articles) == 1
    root = articles[0] if bounded else soup
    for element in root.select('nav, footer, form, aside, [role="navigation"], [role="dialog"]'):
        element.decompose()
    _remove_tag_clouds(root)
    # Some publishers put the principal quotation in a quoteslider.
    # The extractor mistakes its wrapper for an unrelated slideshow.
    # Preserve semantic blockquotes while removing those presentation hints.
    for quote in root.find_all("blockquote"):
        for parent in (quote, *quote.parents):
            if parent is root:
                break
            parent.attrs.pop("class", None)
            parent.attrs.pop("id", None)
    if bounded:
        # The boundary is already settled, so trafilatura's id-based body anchors can
        # only narrow it further - never widen it back - and a publisher whose anchor
        # wraps just the lead loses the rest of the article. verkehrsrundschau.de puts
        # its standfirst in <div id="article-content-wrapper"> and the six sections
        # that follow outside it, which cost ~40% of every story. Classes are kept:
        # they carry the same junk signals without naming a body.
        for element in root.find_all(id=True):
            element.attrs.pop("id", None)
    # Hand over one unambiguous document. trafilatura rejects a bare fragment
    # outright - load_html treats markup whose first 50 characters lack "html" as
    # not-quite-HTML and returns None unless the root has 2+ children - so a single
    # <article> holding one wrapper div extracted to nothing at all, and the caller
    # retired the URL as "no article text even after rendering".
    text = trafilatura.extract(f"<html><body>{root}</body></html>", url=final_url,
                               include_comments=False, include_tables=True)
    text = normalize_text(text or "")
    if len(text) < 200 or sum(c.isalpha() for c in text) < 100:
        return BodyResult("failed", url=final_url, error=NO_ARTICLE_TEXT)
    # These phrases identify subscription teasers, including sites that
    # omit schema.org's access flag. Do not silently call them full text.
    if re.search(r"(?:diesen artikel|den vollständigen artikel) (?:weiter)?lesen "
                 r"(?:sie )?(?:mit|nach)|weiterlesen mit|subscribe to continue reading",
                 text, re.IGNORECASE):
        return BodyResult("unavailable", url=final_url, error="subscription teaser")
    # A redirect to a login page is not a teaser: the article is simply not
    # served. DVZ answers 200 with its subscriber login and no paywall markup at
    # all, so nothing above catches it, and 103 words of "Jetzt 4 Wochen
    # kostenlos testen" was being stored as article text.
    if _is_login_wall(title, text):
        return BodyResult("unavailable", url=final_url, error="subscriber login page")
    return BodyResult("ok", text=text, title=normalize_text(title) if title else None,
                      url=final_url, published_at=published,
                      published_at_raw=published_raw)


def _extract_pdf(data: bytes, final_url: str) -> BodyResult:
    """Text layer of a PDF. Regulators publish press releases and reports this way.

    A PDF with no text layer is a scan, which is a permanent property of that
    file, so it is unavailable rather than retryable. A parser error is neither -
    a truncated download can succeed next time, so that stays failed.

    The creation date is deliberately not read as a publication date. It is the
    date the file was produced, which is the same class of publisher-controlled
    metadata as a sitemap <lastmod> (see CLAUDE.md).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return BodyResult("failed", url=final_url,
                          error="pypdf is not installed; run pip install -r requirements.txt")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            return BodyResult("unavailable", url=final_url, error="password-protected PDF")
        pages = []
        for index, page in enumerate(reader.pages):
            if index >= PDF_MAX_PAGES:
                break
            pages.append(page.extract_text() or "")
        title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    except Exception as exc:  # noqa: BLE001 - any parser error is retryable
        return BodyResult("failed", url=final_url,
                          error=f"PDF parse failed: {type(exc).__name__}: {exc}")
    text = normalize_text("\n".join(pages))
    if len(text) < 200 or sum(c.isalpha() for c in text) < 100:
        return BodyResult("unavailable", url=final_url,
                          error="PDF carries no text layer (scanned image)")
    return BodyResult("ok", text=text, url=final_url,
                      title=normalize_text(str(title)) if title else None)


def _fetch_public(url: str) -> BodyResult:
    """Rung 1: one unauthenticated HTTP request, plus PDF extraction.

    This rung is the only one that sees the status code, content type and final
    URL, so it is what classifies an outcome as unavailable (stop) rather than
    failed (retry). The rungs above it receive markup and nothing else.
    """
    try:
        with requests.get(url, headers=HTML_HEADERS,
                          timeout=(5, BODY_FETCH_TIMEOUT)) as response:
            if response.status_code in (401, 402, 404, 410):
                return BodyResult("unavailable", url=response.url,
                                  error=f"HTTP {response.status_code}")
            response.raise_for_status()
            if urlsplit(response.url).path in ("", "/") and not urlsplit(response.url).query:
                return BodyResult("unavailable", url=response.url,
                                  error="article redirected to homepage")
            media = response.headers.get("Content-Type", "").lower()
            body = response.content
            # Decide on the bytes, not the header. Servers mislabel: a PDF
            # header over HTML would otherwise fail to parse forever, and an
            # unlabelled PDF would be discarded as unsupported.
            if body[:5] == b"%PDF-":
                return _extract_pdf(body, response.url)
            if "pdf" not in media and media and not any(
                    t in media for t in ("text/html", "application/xhtml+xml")):
                return BodyResult("unavailable", url=response.url,
                                  error=f"unsupported content type: {media}")
            return _extract_article(body, response.url)
    except requests.RequestException as exc:
        # ConnectionError means no socket and no name: a dead host, or our own
        # network. ReadTimeout and HTTPError mean the host answered, so they are
        # the publisher's problem and always count toward the attempt cap. An
        # SSLError is a certificate, which is per-host and stays countable too.
        transport = (isinstance(exc, requests.ConnectionError)
                     and not isinstance(exc, requests.exceptions.SSLError))
        return BodyResult("failed", error=f"{type(exc).__name__}: {exc}",
                          transport=transport)


def _fetch_rendered(url: str) -> BodyResult:
    """Rung 3: run the page in a headless browser.

    Only worth paying for when plain HTTP returned markup with no article in it,
    which is what a client-rendered page looks like from requests.
    """
    try:
        from vendor.newscrawler.crawler_playwright import fetch_html_with_playwright
    except ImportError as exc:
        return BodyResult("failed", url=url, error=f"playwright unavailable: {exc}")
    try:
        markup = fetch_html_with_playwright(url, timeout_ms=BODY_FETCH_TIMEOUT * 1000)
    except Exception as exc:  # noqa: BLE001 - a browser can fail in many ways
        return BodyResult("failed", url=url,
                          error=f"browser fetch failed: {type(exc).__name__}: {exc}")
    if not markup:
        return BodyResult("failed", url=url, error="browser returned nothing")
    # The vendored fetcher swallows navigation errors and returns whatever the
    # browser is left showing, which for an unreachable host is a blank document
    # (39 bytes). That is a transport failure, not a page without an article, and
    # the difference matters: the caller retires a URL that renders empty, so a
    # DNS blip must not look like one.
    if not _rendered_anything(markup):
        return BodyResult("failed", url=url,
                          error="browser loaded a blank document (navigation failed)")
    return _extract_article(markup, url)


def _rendered_anything(markup: bytes | str) -> bool:
    """True when the browser actually painted a document with something in it."""
    body = BeautifulSoup(markup, "lxml").body
    return bool(body and (body.find(True) or body.get_text(strip=True)))


def _fetch_subscriber(url: str) -> BodyResult | None:
    """Rung 4: authenticated fetch for a configured paywall.

    Returns None when this rung does not apply - the host has no entry in
    paywalls.json, or its credentials are absent. Checking credentials here keeps
    an unsubscribed site a cheap skip instead of a browser launch that logs in
    with an empty password.
    """
    # Body fetching is normally its own process, so an earlier LLM stage loading
    # dotenv cannot populate this process. Load credentials only when an anonymous
    # request has already declared a paywall; public articles never reach this rung.
    load_dotenv(ROOT / ".env")
    try:
        from vendor.newscrawler.paywall.handler import (fetch_paywall_article,
                                                        get_paywall_cfg)
    except ImportError:
        return None
    cfg = get_paywall_cfg(url)
    if not cfg:
        return None
    if not (os.environ.get(cfg.get("email_env", "")) and
            os.environ.get(cfg.get("password_env", ""))):
        return None
    try:
        markup = fetch_paywall_article(url, cfg)
    except Exception as exc:  # noqa: BLE001 - includes the dead-credentials skip
        return BodyResult("failed", url=url,
                          error=f"paywall fetch failed: {type(exc).__name__}: {exc}")
    if not markup:
        return BodyResult("failed", url=url, error="paywall fetch returned nothing")
    return _extract_article(markup, url)


def fetch_body(url: str) -> BodyResult:
    """Fetch one article body, escalating only on a specific cheaper failure.

    The ladder exists because the expensive rungs are expensive: a browser costs
    seconds and a login costs a subscription. Each one runs only when the rung
    below it failed in the one way that rung can fix, so the ordinary article
    stays a single paced HTTP request.

        1. plain HTTP          - status, content type and final URL
        2. PDF                 - when the response is application/pdf
        3. headless browser    - only on "no usable article text"
        4. subscriber login    - only on a declared paywall, with credentials

    A failed escalation keeps the cheaper diagnosis and records what was tried,
    so the queue's retry semantics do not change just because a browser was used.
    """
    if urlsplit(url).path in ("", "/") and not urlsplit(url).query:
        return BodyResult("unavailable", url=url, error="homepage, not an article URL")

    result = _fetch_public(url)

    if BODY_FETCH_BROWSER and result.status == "failed" and result.error == NO_ARTICLE_TEXT:
        rendered = _fetch_rendered(url)
        if rendered.status == "ok":
            logger.info("[bodies] browser recovered %s", url)
            return rendered
        if rendered.error == NO_ARTICLE_TEXT:
            # Rendered and still empty. Plain HTTP finding no text can mean the
            # page is client-rendered; a browser finding none too means the page
            # is not an article at all - a section listing, a form, a stub. That
            # is permanent, so stop retrying. Otherwise every such URL would cost
            # ~20s of browser time on every run for as long as it is configured.
            return BodyResult("unavailable", url=result.url or url,
                              error="no article text even after rendering")
        # A browser crash or timeout says nothing about the page, so keep it
        # retryable with the cheaper diagnosis intact.
        return BodyResult("failed", url=result.url or url,
                          error=f"{NO_ARTICLE_TEXT}; browser: {rendered.error}")

    if result.status == "unavailable" and result.error == PAYWALL_DECLARED:
        subscriber = _fetch_subscriber(url)
        if subscriber is None:
            return result
        if subscriber.status == "ok":
            logger.info("[bodies] subscriber login recovered %s", url)
            return subscriber
        return BodyResult("unavailable", url=result.url or url,
                          error=f"{PAYWALL_DECLARED}; login: {subscriber.error}")

    return result


def discovery_date_source(payload: dict[str, Any], published: str | None) -> str | None:
    """Provenance label for a date that came from discovery rather than the page.

    Collection now records ``feed``, ``news_sitemap``, ``lastmod`` or
    ``frontpage`` in the hint. Rows collected before 2026-09-11 recorded
    ``discovery`` for all of them, which hid the one distinction a report needs:
    a feed <pubDate> may be printed, a sitemap <lastmod> may not. Those are
    relabelled from ``discovered_via``; a sitemap's flavour cannot be recovered
    after the fact and stays ``sitemap``.
    """
    if published is None:
        return None
    current = payload.get("published_at_source")
    if current not in (None, "discovery", "page"):
        return current
    return {"rss": "feed", "sitemap": "sitemap", "frontpage": "frontpage"}.get(
        payload.get("discovered_via"))


def store_body(conn: sqlite3.Connection, run_id: int, task: sqlite3.Row,
               result: BodyResult) -> int:
    """Enrichment appends a version for content or normalized metadata changes.

    Compare with the latest material, so a genuine A -> B -> A correction is
    preserved. Extraction timestamps and whitespace are not content.  A corrected
    page date is versioned even when body text is stable; otherwise an extractor
    fix could never repair an already-fetched row.
    """
    latest = conn.execute(
        "SELECT * FROM raw_item WHERE source_slug=? AND external_id=? "
        "ORDER BY version DESC LIMIT 1", (task["source_slug"], task["external_id"]),
    ).fetchone()
    if latest is None:
        raise ValueError("body task has no raw item")
    payload = json.loads(latest["payload"])
    hint = json.loads(task["hint_payload"])
    title = result.title or hint.get("title") or latest["title"]
    title = normalize_text(title) if title else None
    body = normalize_text(result.text or "")
    content_hash = digest({"url": task["external_id"], "title": " ".join((title or "").split()),
                           "body_text": " ".join(body.split())})
    published = result.published_at or latest["published_at"]
    if result.published_at:
        published_source = "page"
    elif payload.get("body_text") and payload.get("published_at_source") == "page":
        # A page date read earlier outranks the hint; a refetch that finds none
        # keeps it rather than demoting the row back to a discovery date.
        published_source = "page"
    else:
        published_source = discovery_date_source(payload, published)
    # Compare against what the stored row means, not the literal it carries, so
    # relabelling a legacy ``discovery`` row is not itself a metadata change.
    previous_source = ("page" if payload.get("published_at_source") == "page"
                       else discovery_date_source(payload, latest["published_at"]))
    published_raw = (result.published_at_raw if result.published_at_raw is not None
                     else payload.get("published_at_raw"))
    metadata_changed = (
        published != latest["published_at"] or
        published_source != previous_source or
        published_raw != payload.get("published_at_raw")
    )
    if (payload.get("body_text") and latest["content_hash"] == content_hash
            and not metadata_changed):
        return 0
    fetched = utcnow()
    # The page outranks the discovery hint, whose date may be a <lastmod> the
    # publisher rewrites in bulk. Record which one this is: a date we read from the
    # article is evidence of age, a date we inherited is not.
    payload.update({"title": title, "body_text": body, "body_fetched_at": fetched,
                    "body_url": result.url or latest["url"],
                    "published_at": published,
                    "published_at_source": published_source,
                    "body_extractor": "trafilatura", "body_status": "ok"})
    if published_raw is not None:
        payload["published_at_raw"] = published_raw
    conn.execute(
        "INSERT INTO raw_item (source_slug, source_kind, external_id, version, url, "
        "title, published_at, fetched_at, first_run_id, content_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (latest["source_slug"], latest["source_kind"], latest["external_id"],
         latest["version"] + 1, latest["url"], title, published, fetched,
         run_id, content_hash, json.dumps(payload, ensure_ascii=False)),
    )
    return 1


def run_body_fetch(sources_path: Path, *, kind: str = "news", limit: int = BODY_FETCH_LIMIT,
                   refresh: bool = False, retry_unavailable: bool = False,
                   db_path: Path | None = None, title_gate_client: str | None = None,
                   title_gate_log_root: Path | None = None) -> dict[str, Any]:
    """Fetch a bounded batch, including old hints and retryable failures.

    Normally every configured ``full_text`` item is eligible.  With
    ``title_gate_client``, only explicit keeps from that client's retained JSONL
    decisions are newly queued, regardless of ``content_mode``; existing queued
    title-only failures remain eligible after the originating log is pruned.
    """
    from src.collect import crawled_entries, discovery_enabled, slug_for, url_is_excluded
    from vendor.newscrawler.source_loader import sources

    if limit < 1:
        raise ValueError("body fetch limit must be positive")
    if title_gate_client and kind != "news":
        raise ValueError("title-gate body fetching is only valid for news")
    sources.clear_cache()
    entries = crawled_entries(sources.load_sources(str(sources_path)))
    if not entries:
        raise ValueError(f"no sources in {sources_path}")
    wanted_mode = "title_only" if title_gate_client else "full_text"
    entries = [e for e in entries if content_mode(e) == wanted_mode]
    by_slug = {slug_for(e): e for e in entries}
    tasks = []
    eligible = {slug: set() for slug in by_slug}
    with session(db_path) as conn:
        if title_gate_client:
            from src.title_gate import logged_keeps

            # A source crawled by nothing had its rows deleted; its old keeps
            # stay in the JSONL log and would warn on every run.
            disabled: dict[str, int] = {}
            for decision in logged_keeps(title_gate_client, title_gate_log_root):
                slug, external_id = decision["source"], decision["external_id"]
                if slug not in by_slug:
                    continue
                if not discovery_enabled(by_slug[slug]):
                    disabled[slug] = disabled.get(slug, 0) + 1
                    continue
                row = conn.execute(
                    "SELECT * FROM raw_item WHERE source_slug=? AND external_id=? "
                    "AND source_kind='news' ORDER BY version DESC LIMIT 1",
                    (slug, external_id),
                ).fetchone()
                if row is None:
                    logger.warning("[bodies] title-gate keep has no raw item: %s %s",
                                   slug, external_id)
                    continue
                route = {
                    "client": title_gate_client,
                    "at": decision.get("at"),
                    "prompt_version": decision.get("prompt_version"),
                    "profile_version": decision.get("profile_version"),
                    "reasons": decision.get("reasons", []),
                    "matched_in": decision.get("matched_in", ["title_gate"]),
                    "label": decision.get("label"),
                }
                queue_body(conn, slug, external_id, json.loads(row["payload"]),
                           only_missing=True, title_gate_route=route)
            for slug, count in sorted(disabled.items()):
                logger.info("[bodies] %s is disabled; %d keeps ignored", slug, count)
        for slug, entry in by_slug.items():
            if title_gate_client:
                # A title-only body task can only have been created explicitly.
                # Enumerating the durable queue also keeps retries alive after
                # the short-lived JSONL decision that created it is pruned.
                rows = conn.execute(
                    "SELECT r.* FROM raw_item r JOIN body_fetch b "
                    "ON b.source_slug=r.source_slug AND b.external_id=r.external_id "
                    "WHERE r.source_slug=? AND r.source_kind=? "
                    "AND NOT EXISTS (SELECT 1 FROM raw_item n "
                    "WHERE n.source_slug=r.source_slug AND n.external_id=r.external_id "
                    "AND n.version>r.version)", (slug, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.* FROM raw_item r WHERE r.source_slug=? AND r.source_kind=? "
                    "AND NOT EXISTS (SELECT 1 FROM raw_item n "
                    "WHERE n.source_slug=r.source_slug AND n.external_id=r.external_id "
                    "AND n.version>r.version)", (slug, kind),
                ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if url_is_excluded(row["url"], entry, row["title"]):
                    continue
                # Respect narrowed sitemap sections when enriching the old DB.
                if payload.get("discovered_via") == "sitemap" and not url_matches_dirs(
                        row["url"], entry.get("allowed_dirs")):
                    continue
                eligible[slug].add(row["external_id"])
                if not title_gate_client:
                    queue_body(conn, slug, row["external_id"], payload, only_missing=True)
            for task in conn.execute("SELECT * FROM body_fetch WHERE source_slug=?", (slug,)):
                if task["external_id"] not in eligible[slug]:
                    continue
                if (task["status"] in {"pending", "failed"} or
                    (retry_unavailable and task["status"] == "unavailable") or
                    (refresh and task["status"] == "ok")):
                    tasks.append(task)

    # Oldest attempted first, so a repeatedly failing URL cannot starve the batch.
    tasks.sort(key=lambda t: (t["attempted_at"] or "", t["source_slug"], t["external_id"]))
    # Then round-robin across sources, so the limit is shared between them and no
    # single publisher takes a long unbroken run of requests.
    tasks = interleave_by_source(tasks)
    queued = len(tasks)
    tasks = tasks[:limit]
    summary = {"attempted": 0, "ok": 0, "failed": 0, "unavailable": 0,
               "stored": 0, "deferred": queued - len(tasks), "per_source": [],
               "stopped": None}
    counts = {slug: {"attempted": 0, "ok": 0, "failed": 0, "unavailable": 0,
                     "stored": 0, "errors": []} for slug in by_slug}
    now = utcnow()
    with session(db_path) as conn:
        run_kind = (f"bodies:{kind}:title-gate:{title_gate_client}"
                    if title_gate_client else f"bodies:{kind}")
        run_id = start_run(conn, run_kind, now, now)
    summary["run_id"] = run_id
    host_seen: dict[str, float] = {}
    # Until something in this pass succeeds there is no way to tell a dead host
    # from a dead network, so connection failures get the benefit of the doubt:
    # they cost no attempt, and enough of them in a row end the pass.
    transport_streak = 0
    transport_hosts: set[str] = set()
    for task in tasks:
        logger.info("[bodies] %d/%d %s", summary["attempted"] + 1, len(tasks), task["external_id"])
        pace_host(task["external_id"], host_seen)
        try:
            result = fetch_body(task["external_id"])
        except Exception as exc:
            result = BodyResult("failed", error=f"{type(exc).__name__}: {exc}")
        # ``attempts`` counts consecutive unsuccessful tries; a success resets it
        # and so does a changed hint. Past the cap a failure is retired rather
        # than retried on every run forever. The diagnosis is kept in the error
        # so the row stays recognisable, and --retry-unavailable reopens it -
        # which is how a URL retired under a broken extractor gets recovered
        # once the extractor is fixed.
        attempts = task["attempts"] + 1
        # A connection failure before this pass has fetched anything is not
        # evidence about the URL, so it must not spend the URL's budget. An
        # outage otherwise retires the whole queue: nine passes a day against a
        # cap of five retires everything pending before lunchtime, silently,
        # because ``unavailable`` is a legitimate outcome that fails no stage.
        # Once anything succeeds the network is up and a connection failure is
        # the host's own, so it counts again from here to the end of the pass.
        untried = result.status == "failed" and result.transport and summary["ok"] == 0
        if untried:
            attempts = task["attempts"]
            transport_streak += 1
            transport_hosts.add(urlsplit(task["external_id"]).netloc)
        else:
            transport_streak = 0
            transport_hosts.clear()
        if result.status == "failed" and not untried and attempts >= BODY_FETCH_MAX_ATTEMPTS:
            result = BodyResult("unavailable", url=result.url,
                                error=f"gave up after {attempts} attempts: {result.error}")
        # Commit each outcome before fetching another URL. A stopped run keeps
        # completed work, and every untouched task remains pending/retryable.
        with session(db_path) as conn:
            stored = store_body(conn, run_id, task, result) if result.status == "ok" else 0
            conn.execute(
                "UPDATE body_fetch SET status=?, attempts=?, attempted_at=?, "
                "error=?, last_run_id=? WHERE source_slug=? AND external_id=? "
                "AND discovery_hash=?",
                (result.status, 0 if result.status == "ok" else attempts, utcnow(),
                 result.error, run_id, task["source_slug"], task["external_id"],
                 task["discovery_hash"]),
            )
        source = counts[task["source_slug"]]
        for counter in (summary, source):
            counter["attempted"] += 1
            counter[result.status] += 1
            counter["stored"] += stored
        if result.error:
            source["errors"].append(f"{task['external_id']}: {result.error}")
            logger.warning("[bodies] %s %s: %s", result.status, task["external_id"], result.error)
        if (transport_streak >= TRANSPORT_BREAKER_FAILURES
                and len(transport_hosts) >= TRANSPORT_BREAKER_HOSTS):
            summary["stopped"] = (
                f"{transport_streak} consecutive connection failures across "
                f"{len(transport_hosts)} hosts with nothing fetched - this host has no "
                "network; the queue is untouched and the next pass resumes it")
            logger.warning("[bodies] stopping the pass: %s", summary["stopped"])
            break

    with session(db_path) as conn:
        for slug, counts_for_source in counts.items():
            errors = counts_for_source.pop("errors")
            # ``unavailable`` is a valid terminal item outcome (404, declared
            # paywall, scan without a text layer).  Keep its diagnosis visible,
            # but fail a source/run only for retryable technical failures.
            status = ("failed" if counts_for_source["failed"] else
                      "ok" if counts_for_source["attempted"] else "zero")
            record_source_result(conn, run_id, slug, status,
                                 items_found=counts_for_source["attempted"],
                                 items_stored=counts_for_source["stored"],
                                 error="; ".join(errors) if errors else None)
            summary["per_source"].append({"slug": slug, **counts_for_source})
        remaining = {"pending": 0, "failed": 0, "unavailable": 0}
        for slug, ids in eligible.items():
            for row in conn.execute("SELECT external_id, status FROM body_fetch WHERE source_slug=?", (slug,)):
                if row["external_id"] in ids and row["status"] in remaining:
                    remaining[row["status"]] += 1
        summary["remaining"] = remaining
        finish_run(conn, run_id, "failed" if summary["failed"] else "ok",
                   note=f"{summary['ok']} bodies ok, {summary['stored']} new versions; "
                   f"{summary['failed']} failed, {summary['unavailable']} unavailable; "
                   f"{summary['deferred']} deferred")
    return summary
