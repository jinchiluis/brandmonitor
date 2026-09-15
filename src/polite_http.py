"""Polite discovery traffic: conditional requests and backing off a throttling host.

Measured 2026-09-15, one news discovery pass sent 601 requests and downloaded
229 MB, nine times a day. The rate per host was already gentle (one thread per
source, 0.25-0.85 s apart); the volume was not. faz.net alone took 104 MB a pass,
mostly sitemap files that had not changed since the pass before. Two things here:

``DiscoveryCache`` makes sitemap and feed requests conditional. A 304 does not mean
"skip": it replays the entries the unchanged file yielded last time, so the hints
are identical to a full download. Skipping would change them - an article found by
a titled feed and an untitled sitemap would arrive untitled whenever only the feed
went quiet, and ``src.bodies.queue_body`` requeues a body fetch on any changed hint.

``PoliteAdapter`` counts what a source sends and listens for 429/503. The first
throttle response is waited out (``Retry-After``, else 10 s) and retried; a second
one, or a wait longer than 60 s, ends that source's pass without another request,
and collection reports the source failed so its watermark holds and the next pass
re-covers the window.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter

from src.db import utcnow
from vendor.newscrawler.crawler_html_utils import THROTTLE_STATUSES, HostThrottled

Entry = Tuple[str, Optional[datetime], Optional[str], Optional[str]]
Child = Tuple[str, Optional[datetime]]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class DiscoveryCache:
    """One source's cached sitemap and feed files for one pass.

    ``since`` is the earliest discovery date this pass keeps. A stored file is
    replayed only if it was stored for a window starting no later than that, since
    entries older than its own ``since`` were not kept. New rows collect in
    ``updates`` for the caller to save; nothing here touches the database.
    """

    def __init__(self, rows: Dict[str, Dict[str, Any]], since: datetime):
        self.rows = rows
        self.since = since
        self.updates: Dict[str, Dict[str, Any]] = {}
        self._validators: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self.replayed = 0

    def headers(self, url: str) -> Dict[str, str]:
        row = self.rows.get(url)
        if row is None or datetime.fromisoformat(row["since"]) > self.since:
            return {}
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def replay(self, url: str) -> Tuple[List[Entry], List[Child]]:
        row = self.rows[url]
        self.replayed += 1
        entries = [(u, _dt(when), title, label)
                   for u, when, title, label in json.loads(row["entries"])]
        children = [(u, _dt(when)) for u, when in json.loads(row["children"])]
        return entries, children

    def seen(self, url: str, response: Any) -> None:
        """Note a full response's validators; ``remember`` stores them with its entries."""
        self._validators[url] = (response.headers.get("ETag"),
                                 response.headers.get("Last-Modified"))

    def remember(self, url: str, entries: List[Entry], children: List[Child]) -> None:
        validators = self._validators.pop(url, None)
        # A file without validators is not worth a row, but one that has lost them
        # overwrites its old row, whose headers() then sends nothing.
        if validators is None or (validators == (None, None) and url not in self.rows):
            return
        kept = [(u, _iso(when), title, label) for u, when, title, label in entries
                if when is None or when >= self.since]
        self.updates[url] = {
            "etag": validators[0], "last_modified": validators[1],
            "since": self.since.isoformat(),
            "entries": json.dumps(kept, ensure_ascii=False),
            "children": json.dumps([(u, _iso(when)) for u, when in children]),
        }


def load_discovery_cache(conn: sqlite3.Connection, slug: str) -> Dict[str, Dict[str, Any]]:
    return {row["url"]: dict(row) for row in conn.execute(
        "SELECT url, etag, last_modified, since, entries, children "
        "FROM discovery_cache WHERE source_slug = ?", (slug,))}


def save_discovery_cache(conn: sqlite3.Connection, slug: str,
                         updates: Dict[str, Dict[str, Any]]) -> None:
    fetched = utcnow()
    conn.executemany(
        "INSERT INTO discovery_cache (source_slug, url, etag, last_modified, since, "
        "entries, children, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_slug, url) DO UPDATE SET etag=excluded.etag, "
        "last_modified=excluded.last_modified, since=excluded.since, "
        "entries=excluded.entries, children=excluded.children, "
        "fetched_at=excluded.fetched_at",
        [(slug, url, u["etag"], u["last_modified"], u["since"], u["entries"],
          u["children"], fetched) for url, u in updates.items()])


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Seconds from a Retry-After header (delta or HTTP date), None if absent or bad."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class PoliteAdapter(HTTPAdapter):
    """Transport for one source's discovery sessions in one pass."""

    def __init__(self, *, max_throttles: int = 2, max_wait: float = 60.0,
                 default_wait: float = 10.0, sleep: Callable[[float], None] = time.sleep,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.max_throttles = max_throttles
        self.max_wait = max_wait
        self.default_wait = default_wait
        self.sleep = sleep
        self.requests = 0
        self.not_modified = 0
        self.bytes = 0
        self.throttles = 0
        self.tripped: Optional[str] = None

    def send(self, request, **kwargs):
        if self.tripped:
            raise HostThrottled(self.tripped, request=request)
        while True:
            response = super().send(request, **kwargs)
            self.requests += 1
            # Read here so the count covers every caller, including HEAD probes.
            self.bytes += len(response.content or b"")
            if response.status_code == 304:
                self.not_modified += 1
            if response.status_code not in THROTTLE_STATUSES:
                return response
            self.throttles += 1
            wait = retry_after_seconds(response.headers.get("Retry-After"))
            if self.throttles >= self.max_throttles or (wait or 0) > self.max_wait:
                # Returned as-is, but the source is now an error: whatever this
                # response cost the caller is re-covered by the next pass.
                self.tripped = (f"HTTP {response.status_code} from "
                                f"{urlsplit(request.url).netloc} ({self.throttles} "
                                f"throttle response(s)), stopped this pass")
                return response
            # Waited out and retried once, so a lone 429 does not silently cost
            # the file it answered while the source still reports ok.
            self.sleep(self.default_wait if wait is None else wait)
