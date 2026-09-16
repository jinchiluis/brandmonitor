"""European Parliament legislative procedures: all of them, and what happened to them.

Collection is source-specific, so this collects every procedure of the legislative
types the source entry names (COD, CNS, APP) since its ``since_year`` - the same
set for every client - and leaves relevance to each client's body gate. It began
2026-09-11 as a hand-picked watch-list of 14 files chosen for J&T, which made
collection client-specific: another client could not reuse the corpus, and cutting
a quiet procedure for one client would have removed it for all. What that list
knew is now a client expectation in ``clients/jt-express/labels/``, checked
against the gate rather than deciding collection.

Measured 2026-09-11 (docs/source_coverage.md, "Parliament"): 609 legislative
procedures since 2021, 217 of them still open. A procedure is ~12 KB stored
(p90 22 KB), so the whole open set is about 3 MB and its changes a few MB a year.
One call per procedure returns its events, its current stage and what is already
scheduled - a plenary debate or vote weeks ahead - so a newly scheduled vote
writes a version before it happens.

Procedures are addressed by their interinstitutional reference and never derived
from a CELEX number. GRM turned the customs reform's 52023PC0258 into 2023-0258,
which the API answers with an empty 204, and skipped the file without a word; its
reference is 2023/0156(COD).

Two properties of the API shape the polling, both measured 2026-09-11:

  * **It rate-limits without saying so.** After a burst, 31 of 40 requests came
    back HTTP 429 at one request per second, with no Retry-After header and no
    published limit. So requests are paced slowly, a 429 is retried with a long
    backoff, and a run that keeps being refused stops and leaves the rest for the
    next one rather than hammering. The change feed is no help either: its
    one-day view timed out after 180 s and its one-week view returned nothing.
  * **Most open procedures are dormant.** Only 30 of 82 sampled had any event in
    twelve months. A procedure with recent or scheduled activity is polled every
    run; the rest are swept weekly. A procedure published in the Official Journal
    is law and is never polled again.

The listing returns the whole back catalogue every run, so a procedure that was
already law when we first saw it is stored without a body rather than skipped:
the row is what stops it being fetched again tomorrow, and an item without a body
never reaches a body gate. About 390 of the 609 are in that state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import requests

from src.collect import collector_entry, slug_for, source_watermark_scope
from src.db import (
    finish_run, get_watermark, record_source_result, session, set_watermark,
    start_run, utcnow,
)
from src.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://data.europarl.europa.eu/api/v2"
OEIL_URL = "https://oeil.secure.europarl.europa.eu/oeil/en/procedure-file?reference={}"
COLLECTOR = "ep_procedures"
SOURCE_KIND = "regulatory"
RUN_KIND = "ep_procedures"
REFERENCE = re.compile(r"^(\d{4})/(\d{4})\(([A-Z]{3})\)$")

# The legislative procedure types. Resolutions (RSP) and own-initiative reports
# (INI) are 1,946 further procedures since 2021 and are deliberately out until a
# report shows the need - the same "measure before adding" rule as a new source.
DEFAULT_TYPES = ("COD", "CNS", "APP")
DEFAULT_SINCE_YEAR = 2021
PAGE_SIZE = 500

PROCEDURE_TYPES = {"COD": "ordinary legislative procedure", "CNS": "consultation",
                   "APP": "consent"}
STAGES = {"RDG1": "first reading", "RDG2": "second reading", "RDG3": "third reading"}
# Events after which the procedure is law and nothing further will happen.
FINAL_EVENTS = {"PUBLICATION_OFFICIAL_JOURNAL"}

# A procedure already closed before we ever saw it is history, not news - the same
# first-sight rule as DIP, which keeps the finished 2024 laws out of the corpus.
FIRST_SIGHT_GRACE_DAYS = 60
# No event and nothing scheduled within a year: polled on the weekly sweep only.
DORMANT_AFTER_DAYS = 365
DORMANT_SWEEP_DAYS = 7
# Consecutive refusals before a run gives up and leaves the rest for the next one.
MAX_RATE_LIMIT_FAILURES = 3


class EpError(RuntimeError):
    """The EP Open Data API failed or returned something unusable."""


class EpRateLimited(EpError):
    """The API refused the request rate. Not a fault of the procedure asked for."""


def process_id(reference: str) -> str:
    """``2023/0156(COD)`` -> ``2023-0156``, the id the API expects."""
    match = REFERENCE.match(reference.strip())
    if not match:
        raise ValueError(f"not a procedure reference like 2023/0156(COD): {reference!r}")
    return f"{match.group(1)}-{match.group(2)}"


class EpClient:
    """Slowly paced client for the listing and the per-procedure endpoint."""

    def __init__(self, *, base: str = API_BASE, timeout: int = 120, delay: float = 2.0,
                 retries: int = 4):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/ld+json",
            "User-Agent": "brandmonitor/1.0 (EP Open Data collector)",
        })

    def _get(self, path: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Return the parsed body, or None when the API does not know the id."""
        url = f"{self.base}/{path}"
        last: Optional[str] = None
        refused = False
        for attempt in range(1, self.retries + 1):
            gap = time.monotonic() - self._last_call
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last_call = time.monotonic()
            try:
                response = self._session.get(
                    url, params={**params, "format": "application/ld+json"},
                    timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                # An unknown id is an empty 204, not a 404.
                if response.status_code in (204, 404) or (
                        response.status_code == 200 and not response.content.strip()):
                    return None
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise EpError(f"{path}: HTTP 200 but not JSON") from exc
                if response.status_code == 429:
                    refused = True
                    # No Retry-After is sent, so back off on our own schedule.
                    wait = float(response.headers.get("Retry-After") or 0) or 10.0 * attempt
                    logger.info("[ep] rate limited on %s, waiting %.0fs", path, wait)
                    time.sleep(wait)
                    last = "HTTP 429"
                    continue
                if response.status_code < 500:
                    raise EpError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
                last = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 10))
        message = f"{path}: giving up after {self.retries} attempts - {last}"
        raise EpRateLimited(message) if refused else EpError(message)

    def list_procedures(self, year: int) -> list[dict[str, Any]]:
        """Every procedure of one year, as id, label and type."""
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = self._get("procedures", {"year": year, "offset": offset,
                                            "limit": PAGE_SIZE})
            batch = (data or {}).get("data") or []
            found.extend(batch)
            if len(batch) < PAGE_SIZE:
                return found
            offset += PAGE_SIZE

    def procedure(self, pid: str) -> Optional[dict[str, Any]]:
        """The procedure record, or None when the API does not know the id."""
        data = self._get(f"procedures/{pid}", {})
        records = (data or {}).get("data") or []
        return records[0] if records else None


# ── records ───────────────────────────────────────────────────────────────

def _short(uri: Any) -> str:
    return str(uri or "").rsplit("/", 1)[-1]


def _activity_order(activity: dict[str, Any]) -> tuple[str, str]:
    return (str(activity.get("activity_date") or ""), str(activity.get("id") or ""))


def canonical(record: dict[str, Any]) -> dict[str, Any]:
    """The record with its lists in a fixed order, so the hash sees content only."""
    ordered = dict(record)
    for key in ("consists_of", "was_scheduled_in", "had_participation"):
        if isinstance(ordered.get(key), list):
            ordered[key] = sorted(ordered[key], key=_activity_order)
    if isinstance(ordered.get("created_a_realization_of"), list):
        ordered["created_a_realization_of"] = sorted(ordered["created_a_realization_of"])
    return ordered


def content_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(canonical(record), sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def events(record: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(record.get("consists_of") or [], key=_activity_order)


def _dates(record: dict[str, Any], keys: Iterable[str]) -> list[str]:
    return [str(a.get("activity_date"))[:10] for key in keys
            for a in record.get(key) or [] if a.get("activity_date")]


def last_event_date(record: dict[str, Any]) -> Optional[str]:
    dates = _dates(record, ("consists_of",))
    return max(dates) if dates else None


def is_final(record: dict[str, Any]) -> bool:
    return any(_short(e.get("had_activity_type")) in FINAL_EVENTS for e in events(record))


def is_dormant(record: dict[str, Any], today: date) -> bool:
    """Nothing happened and nothing is scheduled within DORMANT_AFTER_DAYS."""
    dates = _dates(record, ("consists_of", "was_scheduled_in"))
    if not dates:
        return True
    return max(dates) < (today - timedelta(days=DORMANT_AFTER_DAYS)).isoformat()


def _title(record: dict[str, Any], language: str) -> str:
    return str((record.get("process_title") or {}).get(language) or "").strip()


def _activity_line(activity: dict[str, Any]) -> str:
    documents = [_short(doc) for key in ("based_on_a_realization_of",
                                         "recorded_in_a_realization_of",
                                         "documented_by_a_realization_of")
                 for doc in activity.get(key) or []]
    line = f"{str(activity.get('activity_date') or '?')[:10]} {_short(activity.get('had_activity_type'))}"
    return f"{line} - {', '.join(documents)}" if documents else line


def compose_body(reference: str, record: dict[str, Any]) -> str:
    """What the procedure is, where it stands, what is scheduled and what happened."""
    kind = _short(record.get("process_type"))
    stage = _short(record.get("current_stage"))
    lines = [f"European Parliament procedure {reference}, "
             f"{PROCEDURE_TYPES.get(kind, kind or 'unknown type')}"]
    if _title(record, "en"):
        lines.append(f"Title: {_title(record, 'en')}")
    if stage:
        lines.append(f"Current stage: {STAGES.get(stage, stage)} ({stage})")
    agenda = sorted(record.get("was_scheduled_in") or [], key=_activity_order)
    if agenda:
        lines += ["", "On the Parliament's agenda:"] + [_activity_line(a) for a in agenda]
    if events(record):
        lines += ["", "Events:"] + [_activity_line(e) for e in events(record)]
    return "\n".join(lines)


def store_procedure(conn: sqlite3.Connection, run_id: int, slug: str, reference: str,
                    record: dict[str, Any], *, with_body: bool = True) -> int:
    """Store one procedure version; return 1 when a row was written.

    ``with_body=False`` stores the record without composing a body, for a
    procedure that was already law before we first saw it. The row is what stops
    it being fetched again on every run - the Parliament lists its whole back
    catalogue every time - and a body gate reads only items that have a body, so
    it never reaches a client or a report. The record is kept because it is the
    law that applies, should a later stage want it.
    """
    digest = content_hash(record)
    prior = conn.execute(
        "SELECT version, content_hash FROM raw_item WHERE source_slug = ? AND external_id = ?",
        (slug, reference),
    ).fetchall()
    if any(row["content_hash"] == digest for row in prior):
        return 0
    version = max(row["version"] for row in prior) + 1 if prior else 1
    published = last_event_date(record)
    payload: dict[str, Any] = {
        "procedure": canonical(record),
        "published_at_source": "record" if published else None,
    }
    if with_body:
        payload["body_text"] = compose_body(reference, record)
    else:
        payload["closed_before_first_sight"] = True
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_item "
        "(source_slug, source_kind, external_id, version, url, title, "
        " published_at, fetched_at, first_run_id, content_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, SOURCE_KIND, reference, version, OEIL_URL.format(reference),
         _title(record, "de") or _title(record, "en") or reference, published, utcnow(),
         run_id, digest, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.rowcount


def stored_procedures(conn: sqlite3.Connection, slug: str) -> dict[str, dict[str, Any]]:
    """The latest stored record per procedure reference."""
    latest: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
            "SELECT external_id, payload FROM raw_item WHERE source_slug = ? "
            "ORDER BY version", (slug,)):
        try:
            latest[row["external_id"]] = json.loads(row["payload"]).get("procedure") or {}
        except (TypeError, ValueError):
            latest[row["external_id"]] = {}
    return latest


# ── collection ────────────────────────────────────────────────────────────

def due_procedures(listed: Sequence[dict[str, Any]], stored: dict[str, dict[str, Any]],
                   today: date, *, sweep: bool) -> list[tuple[str, Optional[str]]]:
    """(reference, api id) to fetch this run: new, active, and dormant on a sweep.

    The id comes from the listing rather than from the reference, because the
    Parliament splits files: 2021/0211A(COD) and 2021/0211B(COD) are real
    references that no reference-to-id rule of ours would have derived.
    """
    seen: dict[str, Optional[str]] = {}
    for item in listed:
        reference = str(item.get("label") or "")
        if not reference or reference in seen:
            continue
        api_id = str(item.get("process_id") or "").strip()
        if not api_id:
            try:
                api_id = process_id(reference)
            except ValueError:
                api_id = ""
        seen[reference] = api_id or None
    due: list[tuple[str, Optional[str]]] = []
    for reference, api_id in seen.items():
        record = stored.get(reference)
        if record is None:
            due.append((reference, api_id))   # never seen
        elif is_final(record):
            continue                          # law; it will not change again
        elif sweep or not is_dormant(record, today):
            due.append((reference, api_id))
    return due


def run_ep_collection(*, client: Optional[EpClient] = None, db_path: Optional[Path] = None,
                      sources_path: Optional[Path] = None, since_year: Optional[int] = None,
                      sweep: bool = False, today: Optional[date] = None) -> dict[str, Any]:
    """Collect every legislative procedure in scope and store what changed."""
    entry = collector_entry(COLLECTOR, sources_path)
    slug = slug_for(entry)
    types = tuple(entry.get("procedure_types") or DEFAULT_TYPES)
    start_year = int(since_year or entry.get("since_year") or DEFAULT_SINCE_YEAR)
    today = today or date.today()
    scope = source_watermark_scope(SOURCE_KIND, slug) + ":sweep"
    stale_before = (today - timedelta(days=FIRST_SIGHT_GRACE_DAYS)).isoformat()

    api = client or EpClient()
    summary: dict[str, Any] = {
        "kind": RUN_KIND, "source": slug, "types": list(types), "since_year": start_year,
        "listed": 0, "due": 0, "fetched": 0, "stored": 0, "stale": 0, "unknown": [],
        "changed": [], "final": [], "errors": [], "stopped": None, "swept": False,
        "listing_failed": False,
    }
    with session(db_path) as conn:
        run_id = start_run(conn, RUN_KIND, str(start_year), today.isoformat())
        stored = stored_procedures(conn, slug)
        last_sweep = get_watermark(conn, scope)
    summary["run_id"] = run_id
    sweep = sweep or last_sweep is None or (
        today - date.fromisoformat(last_sweep)).days >= DORMANT_SWEEP_DAYS

    listed: list[dict[str, Any]] = []
    try:
        for year in range(start_year, today.year + 1):
            listed += [item for item in api.list_procedures(year)
                       if item.get("process_type") in types]
    except EpError as exc:
        # Without the listing there is nothing to poll, and skipping a year would
        # look like those procedures had ended.
        logger.warning("[ep] listing: %s", exc)
        summary["errors"].append(f"listing: {exc}")
        summary["listing_failed"] = True
        _finish(db_path, run_id, slug, summary)
        return summary
    summary["listed"] = len(listed)

    due = due_procedures(listed, stored, today, sweep=sweep)
    summary["due"] = len(due)
    logger.info("[ep] %d procedures listed, %d due%s", len(listed), len(due),
                " (weekly sweep)" if sweep else "")

    refusals = 0
    for reference, api_id in due:
        if not api_id:
            summary["errors"].append(f"{reference}: the listing carried no process id")
            continue
        try:
            record = api.procedure(api_id)
            refusals = 0
        except EpRateLimited as exc:
            # Not the procedure's fault, but it was not fetched: an error, so the
            # run cannot read as healthy and the sweep below stays due.
            logger.warning("[ep] %s: %s", reference, exc)
            summary["errors"].append(f"{reference}: {exc}")
            refusals += 1
            if refusals >= MAX_RATE_LIMIT_FAILURES:
                summary["stopped"] = f"rate limited {refusals} times in a row: {exc}"
                logger.warning("[ep] %s", summary["stopped"])
                break
            continue
        except EpError as exc:
            logger.warning("[ep] %s: %s", reference, exc)
            summary["errors"].append(f"{reference}: {exc}")
            continue
        if record is None:
            # The API listed it and then did not know it: a listing mismatch worth
            # seeing in the note, not a failure - a real outage fails the listing.
            logger.info("[ep] %s: listed but unknown to the API", reference)
            summary["unknown"].append(reference)
            continue
        if reference not in stored and is_final(record) and (
                last_event_date(record) or "") < stale_before:
            # Already law before we ever looked: history, not this week's news.
            # Stored without a body so it stays out of the gate, and stored at all
            # so the next run does not fetch the whole back catalogue again.
            with session(db_path) as conn:
                store_procedure(conn, run_id, slug, reference, record, with_body=False)
            summary["stale"] += 1
            continue
        with session(db_path) as conn:
            written = store_procedure(conn, run_id, slug, reference, record)
        summary["fetched"] += 1
        summary["stored"] += written
        if written:
            summary["changed"].append(reference)
        if is_final(record):
            summary["final"].append(reference)

    # A sweep counts only when every due procedure was fetched or is known to be
    # unfetchable (listed but unknown to the API). Otherwise a dormant procedure
    # that failed would wait a week. Sweeping again tomorrow instead costs the
    # dormant set once more: 84 procedures on top of 138 active, 2026-09-16.
    if sweep and summary["stopped"] is None and not summary["errors"]:
        with session(db_path) as conn:
            set_watermark(conn, scope, today.isoformat())
        summary["swept"] = True
    _finish(db_path, run_id, slug, summary)
    return summary


def _finish(db_path: Optional[Path], run_id: int, slug: str, summary: dict[str, Any]) -> None:
    with session(db_path) as conn:
        errors = summary["errors"]
        unknown = summary["unknown"]
        status = "failed" if errors else ("ok" if summary["fetched"] else "zero")
        record_source_result(conn, run_id, slug, status, items_found=summary["fetched"],
                             items_stored=summary["stored"],
                             error="; ".join(errors[:3]) if errors else None)
        finish_run(conn, run_id, "failed" if errors else "ok",
                   note=(f"{summary['fetched']} of {summary['due']} due procedures, "
                         f"{summary['stored']} versions stored"
                         + (f"; {len(unknown)} listed but unknown to the API: "
                            f"{', '.join(unknown)}" if unknown else "")
                         + (f"; stopped: {summary['stopped']}" if summary["stopped"] else "")))
