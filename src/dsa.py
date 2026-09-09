"""DSA Transparency Database — daily aggregate collection.

Temu, Shein, AliExpress and TikTok are designated VLOPs, so every content
moderation decision they take is filed here as a statement of reasons. This
collector stores **one aggregate row per platform per day**, not the statements
themselves. That is a deliberate design decision, and the measurements behind it
are worth keeping next to the code because they are not what the API's shape
suggests:

  * Volume forbids item-level storage. Over 2026-08-09..09-07 the four target
    platforms filed 12.5M (Temu), 32.3M (AliExpress), 49.9M (TikTok) and 210k
    (Shein) statements. Amazon Store alone filed 197M.
  * ``territorial_scope: DE`` does not narrow anything, because a pan-EU action
    lists all 27 member states. Measured DE share: Temu 63%, Amazon 69%,
    Shein 86%, AliExpress 89%, TikTok 99%. It is not a German filter.
  * A day's counts are final. ``created_at`` always falls on the same calendar
    day as ``received_date`` (in a 02:00-08:00 UTC window), so there are no late
    arrivals and a completed day never needs re-collecting.
  * ``received_date`` is when the platform *submitted*, not when it acted, and
    platforms batch. Shein filed 209,921 statements on 2026-08-21, 43 on 08-24,
    and nothing on any other day in that 20-day span — so its "~7,000/day"
    average is an artefact of dividing one dump by 30. A daily zero is therefore
    a normal result for a batch filer, not a collection failure, and zeros are
    stored rather than skipped so the difference stays visible.

The consequence for reporting is the same lesson as ``lastmod`` versus
``datePublished`` (see CLAUDE.md): **never present ``received_date`` to a
customer as when something happened.** ``application_date`` and ``content_date``
carry the event; ``received_date`` carries the paperwork. For the same reason a
spike alarm on raw daily volume is close to useless — Temu's own daily count
varies 9x (184k to 1.6M, CV 0.68) with no underlying event.

So the client value is the trend and the composition, which one ``/sql`` GROUP BY
returns in ~600 bytes per day. See docs/source_coverage.md for the endpoint
findings that shaped this, including the two that contradict the published
guidance.

The data is CC BY 4.0. Any client deliverable built on it must carry ATTRIBUTION.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv

from src.config import INPUT_DIR
from src.db import (
    finish_run, get_watermark, record_source_result, session, set_watermark,
    start_run, utcnow,
)
from src.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PLATFORMS = INPUT_DIR / "dsa_platforms.json"
API_BASE = "https://transparency.dsa.ec.europa.eu/api/v1/research"
TOKEN_ENV = "DSA_KEY"
SOURCE_KIND = "dsa"
INDEX = "statement_index"

ATTRIBUTION = (
    'European Commission-DG CONNECT, "Digital Services Act Transparency '
    'Database", Directorate-General for Communications Networks, Content and '
    "Technology, 2023"
)

# Third-party notices under Article 16, as opposed to the platform's own bulk
# automated sweeps. Three to four orders of magnitude rarer and the only subset
# where a human decided something was worth reporting: over 30 days Temu filed
# 12.5M statements but only 9,579 Article 16 notices.
ARTICLE_16 = "SOURCE_ARTICLE_16"


class DsaError(RuntimeError):
    """An API call failed or returned something unusable."""


def load_token(env: Optional[str] = None) -> str:
    """Read the Research API token. Raises rather than degrading to anonymous."""
    load_dotenv()
    token = os.getenv(env or TOKEN_ENV)
    if not token:
        raise DsaError(
            f"{env or TOKEN_ENV} is not set. The Research API token belongs in .env "
            "on the laptop; see docs/dsa_access_request.md."
        )
    return token.strip()


class DsaClient:
    """Thin client over the three endpoints this collector needs.

    Deliberately does not wrap ``/aggregates``. That endpoint accepts an unknown
    attribute, returns HTTP 200, and silently falls back to a date-only total —
    so a typo there costs coverage without ever failing. ``/sql`` rejects a bad
    field with a 422 instead, which is the failure mode we want.
    """

    # The API answers 422 for both a malformed query and a backend that is
    # momentarily unavailable. The first must fail loudly, the second must be
    # retried, and only the message distinguishes them.
    TRANSIENT_422 = ("no alive nodes", "search_phase_execution", "timeout",
                     "circuit_breaking", "too_many_requests")

    def __init__(self, token: Optional[str] = None, *, base: str = API_BASE,
                 timeout: int = 120, delay: float = 1.0, retries: int = 4):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._token = token or load_token()
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last_call = 0.0

    # -- transport ---------------------------------------------------------

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        last: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            self._pace()
            try:
                r = self._session.request(method, url, json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if r.status_code == 200:
                    # A 200 carrying HTML is the site's error page, not a result.
                    if "json" not in r.headers.get("content-type", ""):
                        raise DsaError(f"{path}: HTTP 200 but not JSON "
                                       f"({len(r.content)} bytes) — check the path")
                    return r.json()
                if r.status_code in (401, 403):
                    raise DsaError(f"{path}: HTTP {r.status_code} — the token is "
                                   f"rejected. {r.text[:200]}")
                transient = (
                    r.status_code >= 500
                    or r.status_code == 429
                    or (r.status_code == 422
                        and any(m in r.text.lower() for m in self.TRANSIENT_422))
                )
                if not transient:
                    # 422 otherwise carries the Elasticsearch parse error, which
                    # names the offending field. Surfacing it beats a generic
                    # failure.
                    raise DsaError(f"{path}: HTTP {r.status_code}: {r.text[:400]}")
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code == 429:
                    # Sustained backfill does trip the rate limiter, so widen the
                    # gap for every later call rather than only this retry.
                    self.delay = min(self.delay * 1.5, 10.0)
                    wait = float(r.headers.get("Retry-After") or 0) or 5.0 * attempt
                    logger.warning("[dsa] rate limited; waiting %.0fs and pacing "
                                   "at %.1fs", wait, self.delay)
                    time.sleep(wait)
                    continue
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 10))
        raise DsaError(f"{path}: giving up after {self.retries} attempts — {last}")

    # -- endpoints ---------------------------------------------------------

    def sql(self, query: str) -> List[Sequence[Any]]:
        """Run an Elasticsearch SQL query and return its rows.

        ``territorial_scope`` cannot appear in a SQL WHERE clause — it is mapped
        as ``text`` with no keyword sub-field, and the API answers 422. Use
        ``count()`` for anything scoped to a country.
        """
        return self._request("POST", "/sql", {"query": query}).get("rows", [])

    def count(self, query_string: str) -> int:
        """Exact match count for a Lucene query string."""
        body = {"query": {"query_string": {"query": query_string}}}
        result = self._request("POST", "/count", body)
        if "count" not in result:
            raise DsaError(f"/count returned no count: {str(result)[:200]}")
        return int(result["count"])

    def platforms(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/platforms")


# ── configuration ─────────────────────────────────────────────────────────

def load_platforms(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the tracked-platform list. Ids are stable; names are not."""
    target = Path(path) if path else DEFAULT_PLATFORMS
    entries = json.loads(target.read_text(encoding="utf-8"))
    if not entries:
        raise ValueError(f"no platforms configured in {target}")
    for e in entries:
        if "platform_id" not in e or "name" not in e:
            raise ValueError(f"platform entry needs platform_id and name: {e}")
    return [e for e in entries if e.get("enabled", True)]


def slug_for(entry: Dict[str, Any]) -> str:
    """Per-platform source key, so run_source accounting stays per platform."""
    return entry.get("slug") or f"dsa-{entry['platform_id']}"


# ── collection ────────────────────────────────────────────────────────────

def _day_rows(client: DsaClient, platform_ids: Sequence[int], day: str) -> List[Sequence[Any]]:
    """One SQL call covering every tracked platform for one day.

    Grouped by category and source_type together, so the composition of a day is
    recoverable without a call per combination.
    """
    ids = ",".join(str(i) for i in platform_ids)
    return client.sql(
        f"SELECT platform_id, category, source_type, COUNT(*) AS n FROM {INDEX} "
        f"WHERE platform_id IN ({ids}) AND received_date = '{day}' "
        f"GROUP BY platform_id, category, source_type"
    )


def _day_spread(client: DsaClient, platform_ids: Sequence[int],
                day: str) -> Dict[int, Dict[str, Any]]:
    """How far back the actions in one day's submissions actually reach.

    This is what stops the aggregate being read as daily activity. Shein's
    2026-08-21 submission carried statements spanning 221 distinct
    ``application_date`` values back to 2024-02-26 — a 2.5-year archive filed in
    one go. Without this the row would say "209,921 on 21 August" and a report
    would present a back-catalogue dump as a day of enforcement.
    """
    ids = ",".join(str(i) for i in platform_ids)
    rows = client.sql(
        "SELECT platform_id, MIN(application_date) AS mn, MAX(application_date) AS mx, "
        f"COUNT(DISTINCT application_date) AS spread FROM {INDEX} "
        f"WHERE platform_id IN ({ids}) AND received_date = '{day}' "
        "GROUP BY platform_id"
    )
    out: Dict[int, Dict[str, Any]] = {}
    for pid, mn, mx, spread in rows:
        out[int(pid)] = {
            "earliest": str(mn)[:10] if mn else None,
            # Platforms self-report this field and it is occasionally dated
            # ahead of the submission, so it is recorded rather than trusted.
            "latest": str(mx)[:10] if mx else None,
            "distinct_days": int(spread),
        }
    return out


def build_day_payload(rows: Sequence[Sequence[Any]], platform: Dict[str, Any],
                      day: str, de_total: int, de_article_16: int,
                      spread: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Shape one platform-day aggregate. Pure, so it is testable without the API."""
    pid = platform["platform_id"]
    mine = [r for r in rows if int(r[0]) == pid]

    by_category: Dict[str, int] = {}
    by_source_type: Dict[str, int] = {}
    for _pid, category, source_type, n in mine:
        n = int(n)
        by_category[category or "UNSPECIFIED"] = by_category.get(category or "UNSPECIFIED", 0) + n
        key = source_type or "UNSPECIFIED"
        by_source_type[key] = by_source_type.get(key, 0) + n

    total = sum(by_category.values())
    return {
        "platform_id": pid,
        "platform_name": platform["name"],
        "received_date": day,
        "total": total,
        "territorial_scope_de": de_total,
        "article_16": {
            "all_territories": by_source_type.get(ARTICLE_16, 0),
            "territorial_scope_de": de_article_16,
        },
        # When the actions in this submission were actually taken. A wide spread
        # means the row is a back-catalogue dump, not a day's enforcement.
        "action_dates": spread or {"earliest": None, "latest": None,
                                   "distinct_days": 0},
        # Sorted so the JSON is stable and the content hash is deterministic.
        "by_category": dict(sorted(by_category.items())),
        "by_source_type": dict(sorted(by_source_type.items())),
        "attribution": ATTRIBUTION,
        "api": {"base": API_BASE, "endpoints": ["sql", "count"]},
    }


def _hash(payload: Dict[str, Any]) -> str:
    """Hash the counts only. Attribution and endpoint metadata are not data."""
    basis = {k: payload[k] for k in
             ("platform_id", "received_date", "total", "territorial_scope_de",
              "article_16", "action_dates", "by_category", "by_source_type")}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def store_day(conn: sqlite3.Connection, run_id: int, slug: str,
              payload: Dict[str, Any]) -> int:
    """Insert one platform-day aggregate as a raw item. Returns rows written.

    Versioning follows the same rule as news: a day already stored with the same
    counts is skipped, and a changed count appends a version rather than
    overwriting. In practice restatement does not happen — a day is final once it
    is over — so a second version here is a signal that the assumption broke, not
    routine churn.
    """
    day = payload["received_date"]
    prior = conn.execute(
        "SELECT version, content_hash FROM raw_item "
        "WHERE source_slug = ? AND external_id = ?", (slug, day),
    ).fetchall()
    content_hash = _hash(payload)
    if any(r["content_hash"] == content_hash for r in prior):
        return 0
    version = max(r["version"] for r in prior) + 1 if prior else 1
    if prior:
        logger.warning(
            "[dsa] %s %s: counts changed after the day closed (version %d) — "
            "the 'a completed day is final' assumption may not hold",
            slug, day, version)

    span = payload["action_dates"]["distinct_days"]
    title = (f"{payload['platform_name']} {day}: {payload['total']:,} statements "
             f"({payload['territorial_scope_de']:,} DE-scoped, "
             f"{payload['article_16']['all_territories']:,} Art.16"
             f"{f', actions over {span} days' if span > 1 else ''})")
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_item "
        "(source_slug, source_kind, external_id, version, url, title, "
        " published_at, fetched_at, first_run_id, content_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, SOURCE_KIND, day, version, None, title, day, utcnow(), run_id,
         content_hash, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.rowcount


def _dates(start: date, end: date) -> List[str]:
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def last_complete_day(now: Optional[datetime] = None) -> date:
    """Yesterday in UTC.

    Statements land between 02:00 and 08:00 UTC on their own received_date, so
    today is always partial and collecting it would store a count that is not the
    day's real total.
    """
    return (now or datetime.now(timezone.utc)).date() - timedelta(days=1)


def run_dsa_collection(platforms_path: Optional[Path] = None, *,
                       days: Optional[int] = None,
                       end: Optional[date] = None,
                       client: Optional[DsaClient] = None,
                       db_path: Optional[Path] = None,
                       lookback_days: int = 30) -> Dict[str, Any]:
    """Collect daily aggregates for every configured platform. Returns a summary.

    ``end`` defaults to the last complete day and exists so a historical window
    can be refilled deliberately. It is never advanced past yesterday, because a
    partial day would be stored as though it were a total.
    """
    platforms = load_platforms(platforms_path)
    api = client or DsaClient()

    latest = last_complete_day()
    end = min(end, latest) if end else latest
    scope = f"collection:{SOURCE_KIND}"
    with session(db_path) as conn:
        mark = get_watermark(conn, scope)

    if days is not None:
        start = end - timedelta(days=days - 1)
    elif mark:
        # The watermark is the last day stored, so resume the day after it.
        start = date.fromisoformat(mark) + timedelta(days=1)
    else:
        start = end - timedelta(days=lookback_days - 1)

    # An explicit --days window that starts after the watermark would leave a
    # hole, and advancing the mark over it makes the hole permanent. Same rule
    # as news collection.
    leaves_gap = bool(mark) and start > date.fromisoformat(mark) + timedelta(days=1)
    if leaves_gap:
        logger.warning("[dsa] window starts %s but watermark is %s — the gap "
                       "between them would be skipped, so the watermark will "
                       "not advance", start, mark)

    summary: Dict[str, Any] = {
        "kind": SOURCE_KIND, "platforms": len(platforms), "ok": 0, "zero": 0,
        "failed": 0, "days": 0, "found": 0, "stored": 0,
        "start": start.isoformat(), "end": end.isoformat(), "per_source": [],
    }
    if start > end:
        logger.info("[dsa] nothing to collect: %s is already past %s", mark, end)
        summary["note"] = "up to date"
        return summary

    wanted = _dates(start, end)
    summary["days"] = len(wanted)
    ids = [int(p["platform_id"]) for p in platforms]
    logger.info("[dsa] %d platform(s), %d day(s), %s -> %s",
                len(platforms), len(wanted), start, end)

    # Collected per day so a mid-window failure still leaves whole days stored.
    per_platform: Dict[str, Dict[str, Any]] = {
        slug_for(p): {"slug": slug_for(p), "organization": p["name"],
                      "found": 0, "stored": 0, "errors": []} for p in platforms}
    failed_days: List[str] = []

    with session(db_path) as conn:
        run_id = start_run(conn, SOURCE_KIND, start.isoformat(), end.isoformat())

        for day in wanted:
            try:
                rows = _day_rows(api, ids, day)
                spreads = _day_spread(api, ids, day)
            except DsaError as exc:
                # One bad day must not cost the rest of the window.
                logger.warning("[dsa] %s: %s", day, exc)
                failed_days.append(day)
                for acc in per_platform.values():
                    acc["errors"].append(f"{day}: {exc}")
                continue

            day_ok = True
            for p in platforms:
                slug = slug_for(p)
                acc = per_platform[slug]
                pid = int(p["platform_id"])
                try:
                    base_q = f'platform_id:{pid} AND received_date:"{day}"'
                    de_total = api.count(f"{base_q} AND territorial_scope:DE")
                    de_a16 = api.count(
                        f"{base_q} AND territorial_scope:DE "
                        f"AND source_type:{ARTICLE_16}")
                    payload = build_day_payload(rows, p, day, de_total, de_a16,
                                                spread=spreads.get(pid))
                    acc["stored"] += store_day(conn, run_id, slug, payload)
                    acc["found"] += 1
                except DsaError as exc:
                    logger.warning("[dsa] %s %s: %s", slug, day, exc)
                    acc["errors"].append(f"{day}: {exc}")
                    day_ok = False
            if not day_ok:
                failed_days.append(day)

        for slug, acc in per_platform.items():
            error = "; ".join(acc["errors"][:3]) if acc["errors"] else None
            if error:
                status = "failed"
                summary["failed"] += 1
            else:
                status = "ok" if acc["found"] else "zero"
                summary["ok" if acc["found"] else "zero"] += 1
            summary["found"] += acc["found"]
            summary["stored"] += acc["stored"]
            summary["per_source"].append({**acc, "status": status, "error": error})
            record_source_result(conn, run_id, slug, status,
                                 items_found=acc["found"],
                                 items_stored=acc["stored"], error=error)

        # Advance only over a contiguous run of complete days. A day that failed
        # anywhere in the middle stops the mark there, so the hole is refilled on
        # the next run instead of being skipped forever.
        if not leaves_gap:
            if failed_days:
                earlier = [d for d in wanted if d < min(failed_days)]
                advance_to = earlier[-1] if earlier else None
            else:
                advance_to = wanted[-1]
            if advance_to:
                set_watermark(conn, scope, advance_to)
            summary["watermark_advanced"] = advance_to
        else:
            summary["watermark_advanced"] = None

        summary["failed_days"] = sorted(set(failed_days))
        finish_run(conn, run_id, "ok" if not failed_days else "failed",
                   note=f"{summary['stored']} platform-days stored")
        summary["run_id"] = run_id

    return summary
