"""Bundestag and Bundesrat procedures from the DIP API.

DIP documents every procedure of the Bundestag and the Bundesrat - bills,
resolutions, parliamentary questions with the government's answers, EU documents
forwarded to parliament - as one record per procedure (``Vorgang``) with dated
steps (``Vorgangspositionen``). Measured 2026-09-11 (docs/source_coverage.md,
"Parliament"):

  * Bills are the smallest part of what matters to a parcel carrier. Over 90 days
    the relevant items were two Bundesrat resolutions on third-country
    marketplaces and product safety online, and written questions on customs
    check rates for e-commerce parcels and on undeclared-work inspections in the
    KEP sector. None was in the stored news corpus, and a bills-only query
    (``f.vorgangstyp=Gesetzgebung``, what GRM used) drops every one of them. So
    every procedure type is collected except the procedural ones the source
    entry lists in ``excluded_vorgangstypen``.
  * One raw item per procedure: the native record plus its steps. A new step or
    a changed record writes a new version. For a procedure the new version is
    the news, and which step arrived is in the payload.

Dates are the lastmod lesson again (CLAUDE.md):

  * ``aktualisiert`` is a documentation stamp. It sets the collection window and
    nothing else - it is left out of the content hash and never used as a date.
    In one 14-day window, 1,306 of the 1,858 touched records of the current
    Bundestag were written questions whose own date was a median 340 days old,
    re-indexed to add their abstracts, and all 614 records of earlier terms were
    over a year old.
  * ``datum`` is the date of the procedure's latest step and becomes
    ``published_at`` with ``published_at_source = "record"``. It is not when the
    procedure began or was decided: the Bundesrat adopted its marketplace
    resolution on 2025-07-11, and the record carries 2026-07-16, the federal
    government's reply. The body lists the steps, so the date can be named.
  * A procedure seen for the first time is stored only if its latest step falls
    in the window, less FIRST_SIGHT_GRACE_DAYS for documentation lag. Otherwise
    the re-indexed archive would enter the corpus looking new. A procedure
    already stored takes every change, however old its step.

The body is composed from the record rather than fetched: type, state,
initiative, DIP's subject descriptors, abstract and the dated steps. The
selector and the regulatory body gate read it like any other regulatory body.
Freshly documented records carry descriptors (96-100 %), but a written question
gets its abstract - the question itself - only when it is re-indexed, typically
a year later. The documents behind the steps (an answer, a bill) are fetched by
src/dip_documents.py, only for the procedures a client's gate judged relevant.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import requests
from dotenv import load_dotenv

from src.collect import collector_entry, slug_for, source_watermark_scope
from src.db import (
    advance_watermark, finish_run, get_watermark, record_source_result, session,
    start_run, utcnow,
)
from src.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://search.dip.bundestag.de/api/v1"
WEB_BASE = "https://dip.bundestag.de/vorgang"
# Published by the Bundestag for general use and valid until the end of May 2027.
# A personal key in .env takes precedence.
PUBLIC_API_KEY = "R2BZaee.DjdCyihKZMf8AOjtScubP2EVydegzjmBIQ"
KEY_ENV = "DIP_API_KEY"
COLLECTOR = "dip"
SOURCE_KIND = "regulatory"
RUN_KIND = "dip"
DEFAULT_LOOKBACK_DAYS = 30
# Documentation lag allowed before a new procedure's latest step counts as old.
# An upper bound measured 2026-09-11 on records touched in 14 days: half the
# Kleine Anfragen were touched within 6 days of their latest step and 90 % within
# 67, and those touches include later re-indexing, so first documentation is
# faster still. Too short a grace loses a procedure for good; too long lets a
# little archive in once, while the corpus is young.
FIRST_SIGHT_GRACE_DAYS = 60
# Procedures whose steps are fetched in one request; DIP accepts f.vorgang repeated.
POSITION_BATCH = 50
# Enough to read a bill's course; a long one is cut to its latest steps.
MAX_STEPS_IN_BODY = 30


class DipError(RuntimeError):
    """DIP returned an error or something unusable."""


class DipKeyError(DipError):
    """DIP rejected the API key. A configuration problem, not a quiet day."""


def api_key() -> str:
    load_dotenv()
    return (os.getenv(KEY_ENV) or PUBLIC_API_KEY).strip()


class DipClient:
    """Paced client for the two list endpoints this collector reads."""

    def __init__(self, key: Optional[str] = None, *, base: str = API_BASE,
                 timeout: int = 60, delay: float = 0.3, retries: int = 4):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"ApiKey {key or api_key()}",
            "Accept": "application/json",
            "User-Agent": "brandmonitor/1.0 (DIP public-data collector)",
        })

    def _get(self, path: str, params: Sequence[tuple[str, str]]) -> dict[str, Any]:
        url = f"{self.base}/{path}"
        last: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            gap = time.monotonic() - self._last_call
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last_call = time.monotonic()
            try:
                response = self._session.get(url, params=list(params), timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DipError(f"{path}: HTTP 200 but not JSON") from exc
                if response.status_code in (401, 403):
                    raise DipKeyError(
                        f"{path}: HTTP {response.status_code} - DIP rejected the API key. "
                        f"The public key expires at the end of May 2027; request a personal "
                        f"one (dip.bundestag.de/über-dip/hilfe/api) and set {KEY_ENV} in .env.")
                if response.status_code < 500 and response.status_code != 429:
                    raise DipError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
                last = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 10))
        raise DipError(f"{path}: giving up after {self.retries} attempts - {last}")

    def _pages(self, path: str, params: Sequence[tuple[str, str]]) -> Iterator[list[dict]]:
        cursor: Optional[str] = None
        while True:
            query = list(params) + ([("cursor", cursor)] if cursor else [])
            data = self._get(path, query)
            batch = data.get("documents") or []
            if not batch:
                return
            yield batch
            following = data.get("cursor")
            # DIP hands back the same cursor once everything has been returned.
            if not following or following == cursor:
                return
            cursor = following

    def procedures(self, since: str) -> Iterator[list[dict]]:
        """Pages of procedures whose record changed since ``since`` (ISO datetime)."""
        yield from self._pages("vorgang", [("f.aktualisiert.start", since)])

    def positions(self, procedure_ids: Sequence[str]) -> list[dict]:
        """Every step of the given procedures."""
        steps: list[dict] = []
        for batch in self._pages("vorgangsposition",
                                 [("f.vorgang", pid) for pid in procedure_ids]):
            steps.extend(batch)
        return steps

    def document_text(self, document_id: str) -> dict[str, Any]:
        """One Drucksache with its full text. ``text`` is missing until DIP has
        processed the document, some days after its date."""
        return self._get(f"drucksache-text/{document_id}", [])


# ── records ───────────────────────────────────────────────────────────────

def _without_stamp(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "aktualisiert"}


def step_order(position: dict[str, Any]) -> tuple[str, int]:
    pid = str(position.get("id", ""))
    return (position.get("datum") or "", int(pid) if pid.isdigit() else 0)


def content_hash(procedure: dict[str, Any], positions: Sequence[dict[str, Any]]) -> str:
    """Hash everything DIP says about a procedure except its documentation stamps."""
    basis = {"vorgang": _without_stamp(procedure),
             "positionen": [_without_stamp(p) for p in sorted(positions, key=step_order)]}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def web_url(procedure: dict[str, Any]) -> str:
    """The procedure's DIP page, which ends in its id like DIP's own links."""
    slug = re.sub(r"\W+", "-", (procedure.get("titel") or "").casefold()).strip("-")
    return f"{WEB_BASE}/{slug[:80].rstrip('-') or 'vorgang'}/{procedure['id']}"


def _plain(value: Any) -> str:
    """DIP abstracts carry HTML line breaks and entities."""
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _step_line(position: dict[str, Any]) -> str:
    source = position.get("fundstelle") or {}
    parts = [f"{position.get('datum') or '?'} {position.get('vorgangsposition') or ''}"
             f" ({position.get('zuordnung') or '?'})"]
    document = " ".join(str(v) for v in (source.get("dokumentart"),
                                          source.get("dokumentnummer")) if v)
    if source.get("frage_nummer"):
        document += f", Frage {source['frage_nummer']}"
    if document:
        parts.append(document)
    if position.get("abstract"):
        parts.append(_plain(position["abstract"]).replace("\n", " "))
    parts += [b["beschlusstenor"] for b in position.get("beschlussfassung") or []
              if b.get("beschlusstenor")]
    ministries = [r["titel"] for r in position.get("ressort") or [] if r.get("titel")]
    if ministries:
        parts.append(", ".join(ministries))
    people = [f"{a['aktivitaetsart']}: {a['titel']}"
              for a in position.get("aktivitaet_anzeige") or []
              if a.get("aktivitaetsart") and a.get("titel")]
    if people:
        # A group's Kleine Anfrage lists every signatory; three say who asked.
        parts.append("; ".join(people[:3]) + ("; ..." if len(people) > 3 else ""))
    return " - ".join(parts)


def compose_body(procedure: dict[str, Any], positions: Sequence[dict[str, Any]]) -> str:
    """The procedure's own documentation as text: what it is and what happened."""
    lines = [f"{procedure.get('vorgangstyp') or 'Vorgang'}, "
             f"{procedure.get('wahlperiode') or '?'}. Wahlperiode"]
    for label, value in (
            ("Stand", procedure.get("beratungsstand")),
            ("Initiative", ", ".join(procedure.get("initiative") or [])),
            ("Sachgebiete", ", ".join(procedure.get("sachgebiet") or [])),
            ("Deskriptoren", ", ".join(d["name"] for d in procedure.get("deskriptor") or []
                                        if d.get("name")))):
        if value:
            lines.append(f"{label}: {value}")
    if procedure.get("abstract"):
        lines += ["", "Zusammenfassung:", _plain(procedure["abstract"])]
    steps = sorted(positions, key=step_order)
    if steps:
        lines += ["", "Verlauf:"]
        if len(steps) > MAX_STEPS_IN_BODY:
            lines.append(f"({len(steps) - MAX_STEPS_IN_BODY} frühere Schritte)")
        lines += [_step_line(p) for p in steps[-MAX_STEPS_IN_BODY:]]
    return "\n".join(lines)


def store_procedure(conn: sqlite3.Connection, run_id: int, slug: str,
                    procedure: dict[str, Any], positions: Sequence[dict[str, Any]]) -> int:
    """Store one procedure version; return 1 when a row was written."""
    external_id = str(procedure["id"])
    digest = content_hash(procedure, positions)
    prior = conn.execute(
        "SELECT version, content_hash FROM raw_item WHERE source_slug = ? AND external_id = ?",
        (slug, external_id),
    ).fetchall()
    if any(row["content_hash"] == digest for row in prior):
        return 0
    version = max(row["version"] for row in prior) + 1 if prior else 1
    steps = sorted(positions, key=step_order)
    payload = {
        "vorgang": procedure,
        "positionen": steps,
        "body_text": compose_body(procedure, steps),
        "published_at_source": "record" if procedure.get("datum") else None,
    }
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_item "
        "(source_slug, source_kind, external_id, version, url, title, "
        " published_at, fetched_at, first_run_id, content_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, SOURCE_KIND, external_id, version, web_url(procedure), procedure.get("titel"),
         procedure.get("datum"), utcnow(), run_id, digest,
         json.dumps(payload, ensure_ascii=False)),
    )
    return cur.rowcount


def _known(conn: sqlite3.Connection, slug: str, ids: Sequence[str]) -> set[str]:
    marks = ",".join("?" for _ in ids)
    return {row["external_id"] for row in conn.execute(
        f"SELECT DISTINCT external_id FROM raw_item WHERE source_slug = ? "
        f"AND external_id IN ({marks})", (slug, *ids))}


# ── collection ────────────────────────────────────────────────────────────

def run_dip_collection(*, days: Optional[float] = None,
                       lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                       client: Optional[DipClient] = None,
                       db_path: Optional[Path] = None,
                       sources_path: Optional[Path] = None,
                       now: Optional[datetime] = None) -> dict[str, Any]:
    """Collect procedures changed since the watermark. Returns a summary.

    A DipKeyError closes the run as failed and is raised again: a rejected key
    is a configuration problem the caller must see, not a zero-yield day.
    """
    entry = collector_entry(COLLECTOR, sources_path)
    slug = slug_for(entry)
    excluded = set(entry.get("excluded_vorgangstypen") or [])
    scope = source_watermark_scope(SOURCE_KIND, slug)
    now = now or datetime.now(timezone.utc)
    with session(db_path) as conn:
        mark = get_watermark(conn, scope)

    if days is not None:
        start = now - timedelta(days=days)
    elif mark:
        start = datetime.fromisoformat(mark)
    else:
        start = now - timedelta(days=lookback_days)
    # An explicit window beginning after the watermark leaves a hole; collect it,
    # but do not claim the missing interval. Same rule as the other collectors.
    leaves_gap = bool(mark) and start > datetime.fromisoformat(mark)
    # DIP's clock and ours need not agree to the minute. A day of overlap costs a
    # re-listing that the content hash turns into no writes.
    query_day = (start - timedelta(days=1)).date()
    first_sight_floor = (query_day - timedelta(days=FIRST_SIGHT_GRACE_DAYS)).isoformat()

    api = client or DipClient()
    summary: dict[str, Any] = {
        "kind": RUN_KIND, "source": slug, "start": query_day.isoformat(),
        "end": now.isoformat(), "listed": 0, "excluded": 0, "stale": 0, "found": 0,
        "stored": 0, "failed_batches": 0, "errors": [], "watermark_advanced": None,
    }
    with session(db_path) as conn:
        run_id = start_run(conn, RUN_KIND, query_day.isoformat(), now.isoformat())
    summary["run_id"] = run_id

    fatal: Optional[DipKeyError] = None
    try:
        for page in api.procedures(f"{query_day.isoformat()}T00:00:00"):
            summary["listed"] += len(page)
            wanted = [p for p in page if p.get("vorgangstyp") not in excluded]
            summary["excluded"] += len(page) - len(wanted)
            for index in range(0, len(wanted), POSITION_BATCH):
                batch = wanted[index:index + POSITION_BATCH]
                with session(db_path) as conn:
                    known = _known(conn, slug, [str(p["id"]) for p in batch])
                fresh = [p for p in batch if str(p["id"]) in known
                         or (p.get("datum") or "") >= first_sight_floor]
                summary["stale"] += len(batch) - len(fresh)
                if not fresh:
                    continue
                try:
                    steps = api.positions([str(p["id"]) for p in fresh])
                except DipKeyError:
                    raise
                except DipError as exc:
                    # Storing these without their steps would write a version that
                    # the next complete fetch replaces; leave them for the retry.
                    logger.warning("[dip] steps for %d procedures: %s", len(fresh), exc)
                    summary["failed_batches"] += 1
                    summary["errors"].append(f"steps: {exc}")
                    continue
                by_procedure: dict[str, list[dict]] = {}
                for step in steps:
                    by_procedure.setdefault(str(step.get("vorgang_id")), []).append(step)
                # Committed per batch, so a stopped run keeps its completed work.
                with session(db_path) as conn:
                    for procedure in fresh:
                        summary["stored"] += store_procedure(
                            conn, run_id, slug, procedure, by_procedure.get(str(procedure["id"]), []))
                summary["found"] += len(fresh)
    except DipKeyError as exc:
        fatal = exc
        summary["errors"].append(str(exc))
    except DipError as exc:
        logger.warning("[dip] listing: %s", exc)
        summary["errors"].append(f"listing: {exc}")

    with session(db_path) as conn:
        errors = summary["errors"]
        status = "failed" if errors else ("ok" if summary["found"] else "zero")
        record_source_result(conn, run_id, slug, status, items_found=summary["found"],
                             items_stored=summary["stored"],
                             error="; ".join(errors[:3]) if errors else None)
        # The watermark is this run's start: anything DIP touches while the run
        # is listing is picked up next time rather than skipped.
        if not errors and not leaves_gap:
            advance_watermark(conn, scope, now.isoformat())
            summary["watermark_advanced"] = now.isoformat()
        finish_run(conn, run_id, "failed" if errors else "ok",
                   note=(f"{summary['found']} procedures, {summary['stored']} versions stored; "
                         f"{summary['excluded']} procedural, {summary['stale']} "
                         "re-indexed archive skipped"))
    if fatal is not None:
        raise fatal
    return summary
