"""EU Safety Gate weekly alert collection.

Safety Gate publishes an XML index of weekly reports and one XML document per
report.  Alerts are structured regulatory records, not articles: their native
fields are kept in ``raw_item.payload`` under ``source_kind=safety_gate``.

Collection remains client-independent.  ``select_client_alerts`` is the separate
pilot-view helper: it applies the agreed Germany-notified + Chinese-origin rule
and annotates online-trader matches from a versioned client profile.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from src.db import (
    finish_run, get_watermark, record_source_result, session, set_watermark,
    start_run, utcnow,
)
from src.logger import get_logger
from src.profile import ClientProfile, load_profile

logger = get_logger(__name__)

LIST_URL = (
    "https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/"
    "list/xml/en"
)
SOURCE_KIND = "safety_gate"
SOURCE_SLUG = "eu-safety-gate"
WATERMARK_SCOPE = f"collection:{SOURCE_KIND}"
DEFAULT_LOOKBACK_WEEKS = 12

_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_REPORT_ID = re.compile(r"/([0-9]+)(?:\?|$)")


class SafetyGateError(RuntimeError):
    """The Safety Gate endpoint returned an unusable response."""


@dataclass(frozen=True)
class WeeklyReport:
    report_id: int
    reference: str
    publication_date: str
    url: str
    language: str
    year: int
    week: int


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None or element.attrib.get(_XSI_NIL, "").lower() == "true":
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def _parse_eu_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except (TypeError, ValueError) as exc:
        raise SafetyGateError(f"invalid Safety Gate date {value!r}") from exc


def _xml_root(data: bytes | str, label: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SafetyGateError(f"{label}: invalid XML: {exc}") from exc


def parse_report_list(data: bytes | str) -> list[WeeklyReport]:
    """Parse and chronologically order the weekly-report index."""
    root = _xml_root(data, "weekly report list")
    reports: list[WeeklyReport] = []
    seen: set[int] = set()
    for node in root.iter():
        if _tag(node) != "weeklyReport":
            continue
        values = {_tag(child): _text(child) for child in node}
        url = values.get("URL")
        reference = values.get("reference")
        published = values.get("publicationDate")
        if not url or not reference or not published:
            raise SafetyGateError(
                "weekly report entry is missing URL, reference, or publicationDate"
            )
        match = _REPORT_ID.search(urlsplit(url).path)
        if not match:
            raise SafetyGateError(f"cannot find report id in {url!r}")
        report_id = int(match.group(1))
        if report_id in seen:
            raise SafetyGateError(f"duplicate weekly report id {report_id}")
        seen.add(report_id)
        try:
            year = int(values.get("year") or 0)
            week = int(values.get("week") or 0)
        except ValueError as exc:
            raise SafetyGateError(f"invalid year/week in {reference}") from exc
        reports.append(WeeklyReport(
            report_id=report_id,
            reference=reference,
            publication_date=_parse_eu_date(published),
            url=url,
            language=values.get("report_language") or "en",
            year=year,
            week=week,
        ))
    if not reports:
        raise SafetyGateError("weekly report list contains no reports")
    return sorted(reports, key=lambda r: (r.publication_date, r.report_id))


def _native_value(element: ET.Element) -> Any:
    """Turn an alert field into JSON without discarding unknown nested fields."""
    if element.attrib.get(_XSI_NIL, "").lower() == "true":
        return None
    children = list(element)
    if not children:
        return _text(element)
    grouped: dict[str, list[Any]] = {}
    for child in children:
        grouped.setdefault(_tag(child), []).append(_native_value(child))
    return {
        key: values if len(values) > 1 else values[0]
        for key, values in grouped.items()
    }


def parse_report_detail(data: bytes | str, report: WeeklyReport) -> list[dict[str, Any]]:
    """Parse one report, preserving every native notification field."""
    root = _xml_root(data, report.reference)
    root_values = {_tag(child): _text(child) for child in root
                   if _tag(child) != "notifications"}
    stated_date = root_values.get("report_date")
    if stated_date and _parse_eu_date(stated_date) != report.publication_date:
        raise SafetyGateError(
            f"{report.reference}: list date {report.publication_date} does not match "
            f"detail date {stated_date}"
        )

    alerts: list[dict[str, Any]] = []
    case_numbers: set[str] = set()
    for node in root:
        if _tag(node) != "notifications":
            continue
        payload = {_tag(child): _native_value(child) for child in node}
        case_number = payload.get("caseNumber")
        if not isinstance(case_number, str) or not case_number.strip():
            raise SafetyGateError(f"{report.reference}: notification has no caseNumber")
        if case_number in case_numbers:
            raise SafetyGateError(
                f"{report.reference}: duplicate caseNumber {case_number}"
            )
        case_numbers.add(case_number)

        pictures = payload.get("pictures")
        if pictures is None:
            payload["pictures"] = []
        elif isinstance(pictures, dict):
            value = pictures.get("picture", [])
            payload["pictures"] = value if isinstance(value, list) else [value]
        elif not isinstance(pictures, list):
            payload["pictures"] = [pictures]

        payload["notificationType"] = node.attrib.get(_XSI_TYPE)
        payload["report"] = asdict(report)
        alerts.append(payload)
    return alerts


class SafetyGateClient:
    """Small, paced client for the public XML download endpoints."""

    def __init__(self, *, list_url: str = LIST_URL, timeout: int = 60,
                 delay: float = 0.25, retries: int = 4):
        self.list_url = list_url
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/xml,text/xml",
            "User-Agent": "brandmonitor/1.0 (EU Safety Gate public-data collector)",
        })

    def _get(self, url: str) -> bytes:
        last: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            gap = time.monotonic() - self._last_call
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last_call = time.monotonic()
            try:
                response = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code == 200:
                    if "xml" not in content_type and not response.content.lstrip().startswith(b"<"):
                        raise SafetyGateError(
                            f"{url}: HTTP 200 but not XML ({content_type or 'unknown type'})"
                        )
                    return response.content
                if response.status_code < 500 and response.status_code != 429:
                    raise SafetyGateError(
                        f"{url}: HTTP {response.status_code}: {response.text[:300]}"
                    )
                last = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 10))
        raise SafetyGateError(
            f"{url}: giving up after {self.retries} attempts — {last}"
        )

    def list_reports(self) -> list[WeeklyReport]:
        return parse_report_list(self._get(self.list_url))

    def get_report(self, report: WeeklyReport) -> list[dict[str, Any]]:
        return parse_report_detail(self._get(report.url), report)


def _content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _title(payload: dict[str, Any]) -> str:
    identity = payload.get("brand") or payload.get("name") or payload.get("product")
    product = payload.get("product")
    risk = payload.get("riskType") or payload.get("level")
    parts = [str(value).strip() for value in (identity, product, risk) if value]
    # Do not repeat the product when it was also the fallback identity.
    unique = list(dict.fromkeys(parts))
    return " — ".join(unique) or str(payload["caseNumber"])


def store_alert(conn: sqlite3.Connection, run_id: int,
                payload: dict[str, Any]) -> int:
    """Store one immutable alert version; return 1 when a row was written."""
    external_id = str(payload["caseNumber"]).strip()
    prior = conn.execute(
        "SELECT version, content_hash FROM raw_item "
        "WHERE source_slug = ? AND external_id = ?",
        (SOURCE_SLUG, external_id),
    ).fetchall()
    content_hash = _content_hash(payload)
    if any(row["content_hash"] == content_hash for row in prior):
        return 0
    version = max(row["version"] for row in prior) + 1 if prior else 1
    report = payload["report"]
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_item "
        "(source_slug, source_kind, external_id, version, url, title, "
        " published_at, fetched_at, first_run_id, content_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SOURCE_SLUG, SOURCE_KIND, external_id, version, payload.get("reference"),
         _title(payload), report["publication_date"], utcnow(), run_id,
         content_hash, json.dumps(payload, ensure_ascii=False)),
    )
    return cur.rowcount


def _choose_reports(reports: list[WeeklyReport], mark: Optional[str], *,
                    weeks: Optional[int], end: Optional[date],
                    lookback_weeks: int) -> tuple[list[WeeklyReport], bool]:
    """Return reports to fetch and whether they directly continue the watermark."""
    eligible = [r for r in reports
                if end is None or date.fromisoformat(r.publication_date) <= end]
    if weeks is not None:
        chosen = eligible[-weeks:]
    elif mark is not None:
        try:
            mark_id = int(mark)
        except ValueError as exc:
            raise SafetyGateError(f"invalid Safety Gate watermark {mark!r}") from exc
        positions = {report.report_id: index for index, report in enumerate(reports)}
        if mark_id not in positions:
            raise SafetyGateError(
                f"watermark report {mark_id} is absent from the official report list"
            )
        chosen = [r for r in eligible if positions[r.report_id] > positions[mark_id]]
    else:
        chosen = eligible[-lookback_weeks:]

    if not chosen:
        return [], False
    if mark is None:
        return chosen, True
    positions = {report.report_id: index for index, report in enumerate(reports)}
    mark_id = int(mark)
    after_mark = [r for r in chosen if positions[r.report_id] > positions[mark_id]]
    contiguous = bool(after_mark) and positions[after_mark[0].report_id] == positions[mark_id] + 1
    return chosen, contiguous


def run_safety_gate_collection(*, weeks: Optional[int] = None,
                               end: Optional[date] = None,
                               lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
                               max_reports: Optional[int] = None,
                               client: Optional[SafetyGateClient] = None,
                               db_path: Optional[Path] = None) -> dict[str, Any]:
    """Collect weekly reports, with versioning and a resumable report watermark."""
    if weeks is not None and weeks < 1:
        raise ValueError("weeks must be positive")
    if lookback_weeks < 1:
        raise ValueError("lookback_weeks must be positive")
    if max_reports is not None and max_reports < 1:
        raise ValueError("max_reports must be positive")

    api = client or SafetyGateClient()
    reports = api.list_reports()
    with session(db_path) as conn:
        mark = get_watermark(conn, WATERMARK_SCOPE)
    chosen, contiguous = _choose_reports(
        reports, mark, weeks=weeks, end=end, lookback_weeks=lookback_weeks)
    if max_reports is not None:
        chosen = chosen[:max_reports]

    summary: dict[str, Any] = {
        "kind": SOURCE_KIND, "source": SOURCE_SLUG, "reports": len(chosen),
        "reports_ok": 0, "reports_failed": 0, "found": 0, "stored": 0,
        "failed_reports": [], "watermark_advanced": None,
    }
    if not chosen:
        summary["note"] = "up to date"
        summary["end"] = reports[-1].publication_date
        return summary

    summary["start"] = chosen[0].publication_date
    summary["end"] = chosen[-1].publication_date
    errors: list[str] = []
    with session(db_path) as conn:
        run_id = start_run(conn, SOURCE_KIND, summary["start"], summary["end"])
    summary["run_id"] = run_id

    # Fetch outside a database transaction and commit each complete report on its
    # own. A 1,100-report historical run can then resume after a stopped process,
    # and no network timeout holds SQLite's single writer lock.
    can_advance = contiguous
    for report in chosen:
        try:
            alerts = api.get_report(report)
            with session(db_path) as conn:
                for payload in alerts:
                    summary["stored"] += store_alert(conn, run_id, payload)
                is_after_mark = mark is None or report.report_id > int(mark)
                if can_advance and is_after_mark:
                    set_watermark(conn, WATERMARK_SCOPE, str(report.report_id))
                    summary["watermark_advanced"] = report.report_id
            summary["found"] += len(alerts)
            summary["reports_ok"] += 1
        except SafetyGateError as exc:
            message = f"{report.reference}: {exc}"
            logger.warning("[safety_gate] %s", message)
            errors.append(message)
            summary["reports_failed"] += 1
            summary["failed_reports"].append(report.reference)
            # Later reports may still be stored, but advancing over this hole would
            # make the failed report disappear from the next incremental run.
            can_advance = False

    with session(db_path) as conn:
        status = "failed" if errors else ("ok" if summary["found"] else "zero")
        record_source_result(
            conn, run_id, SOURCE_SLUG, status,
            items_found=summary["found"], items_stored=summary["stored"],
            error="; ".join(errors[:3]) if errors else None,
        )

        finish_run(
            conn, run_id, "failed" if errors else "ok",
            note=(f"{summary['reports_ok']} reports fetched, "
                  f"{summary['stored']} alerts stored"),
        )
    return summary


# --- Reading an alert -------------------------------------------------------
#
# Safety Gate records carry no article text, so a body is composed from the
# record itself - the same approach as `compose_body` in src/dip.py and
# src/ep_procedures.py, and for the same reason: an LLM summariser would add
# cost and a fabrication risk to fields that are already prose.
#
# Unlike those two, the body is composed when the alert is read rather than
# stored in the payload. They must store it because the body gate reads
# `payload.body_text` straight out of SQL; Safety Gate skips both gates (see
# docs/selection_and_assessment.md, "Safety Gate"), so nothing queries it. That
# keeps one copy of the text, lets a template fix reach the alerts already
# stored, and needs no backfill.

_MEASURE_LABELS = (
    # Two operator spellings, both live: 505 and 358 of the 639 alerts stored on
    # 2026-09-12. A measure ordered by an authority and one a company notified
    # voluntarily are the same field to a reader, so they are not distinguished.
    ("operator", "Type of economic operator taking notified measure(s):"),
    ("operator", "Type of economic operator to whom the measure(s) were ordered:"),
    ("category", "Category of measure(s):"),
    ("in_force", "Date of entry into force:"),
)
_MEASURE_LABEL = re.compile(
    "|".join(f"(?P<{field}_{index}>{re.escape(label)})"
             for index, (field, label) in enumerate(_MEASURE_LABELS)))


def parse_measures(text: Optional[str]) -> list[dict[str, Optional[str]]]:
    """Split the run-together `measures` string into one dict per measure.

    The field arrives with no separators at all - label, value, next label - and
    an alert may carry several measures: 29 of the 50 in the 2026-09-12 pilot
    review did, so reading only the first undercounts marketplace removals.
    Labels are located rather than split on, because the string does not reliably
    begin with one and an unknown spelling must cost only its own field.
    A record that states a measure with no labels at all becomes a single measure
    with only a category.
    """
    if not text or not text.strip():
        return []
    found = list(_MEASURE_LABEL.finditer(text))
    if not found:
        return [{"operator": None, "category": text.strip(), "in_force": None}]

    measures: list[dict[str, Optional[str]]] = []
    current: dict[str, Optional[str]] = {}
    for index, match in enumerate(found):
        field = match.lastgroup.rsplit("_", 1)[0]
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        # A repeated field starts the next measure; the API emits them in order.
        if field in current:
            measures.append(current)
            current = {}
        current[field] = text[match.end():end].strip() or None
    if current:
        measures.append(current)
    for measure in measures:
        measure.setdefault("operator", None)
        measure.setdefault("category", None)
        # The API writes a literal "Unknown" here - 267 of the 962 measures stored
        # on 2026-09-12 - and anything it cannot date is no date at all. Keeping
        # the word would print "in force Unknown" and would let a reader sort or
        # compare it as if it were one.
        in_force = measure.get("in_force")
        try:
            measure["in_force"] = _parse_eu_date(in_force) if in_force else None
        except SafetyGateError:
            measure["in_force"] = None
    return measures


def _clean(value: Any) -> Optional[str]:
    """Payload text as written, with HTML entities resolved (\"Y&amp;H\")."""
    text = html.unescape(str(value)).strip() if value is not None else ""
    return text or None


_BODY_FIELDS = (
    ("Product", "product"), ("Brand", "brand"), ("Model or type", "type_numberOfModel"),
    ("Batch", "batchNumber"), ("Barcode", "barcode"), ("Category", "category"),
    ("Risk", "riskType"), ("Notified by", "notifyingCountry"),
    ("Country of origin", "countryOfOrigin"),
)


def compose_body(payload: dict[str, Any]) -> str:
    """The alert as readable text, for the assessor and for `safety-gate-view`."""
    report = payload.get("report") or {}
    header = f"EU Safety Gate alert {payload.get('caseNumber')}"
    if _clean(payload.get("level")):
        header += f", {_clean(payload['level'])}"
    if report.get("publication_date"):
        header += f", published {report['publication_date']}"
    lines = [header]
    lines += [f"{label}: {_clean(payload.get(key))}"
              for label, key in _BODY_FIELDS if _clean(payload.get(key))]

    # Kept verbatim rather than parsed into platform names: the field is free
    # text ("Other(Temu: XU3531330) /"), and the seller reference in it is the
    # only handle a customer has for finding the listing.
    if _clean(payload.get("onlineTrader")):
        lines.append("Sold online through: "
                     + _clean(payload["onlineTrader"]).rstrip(" /"))
    if _clean(payload.get("URLrecall")):
        lines.append(f"Recall notice: {_clean(payload['URLrecall'])}")

    for label, key in (("Description", "description"), ("Danger", "danger")):
        if _clean(payload.get(key)):
            lines += ["", f"{label}:", _clean(payload[key])]

    measures = parse_measures(payload.get("measures"))
    if measures:
        lines += ["", "Measures:"]
        for measure in measures:
            text = measure.get("category") or "measure not stated"
            if measure.get("operator"):
                text += f" (by the {measure['operator'].lower()})"
            if measure.get("in_force"):
                text += f", in force {measure['in_force']}"
            lines.append(text)
    return "\n".join(lines)


def _is_pilot_geography(payload: dict[str, Any]) -> bool:
    notifier = str(payload.get("notifyingCountry") or "").strip().casefold()
    origin = str(payload.get("countryOfOrigin") or "").strip().casefold()
    return notifier == "germany" and origin in {
        "china", "people's republic of china", "people’s republic of china",
    }


def client_match_reasons(payload: dict[str, Any],
                         profile: ClientProfile) -> tuple[str, ...]:
    """Reasons for a pilot-view alert, or empty when geography excludes it."""
    if not _is_pilot_geography(payload):
        return ()
    reasons = ["notifying_country:Germany", "origin:China"]
    trader = str(payload.get("onlineTrader") or "")
    reasons.extend(
        f"online_trader:{reason.removeprefix('brand:')}"
        for reason in profile.reasons_for(trader)
        if reason.startswith("brand:")
    )
    return tuple(reasons)


def select_client_alerts(client_slug: str, *, db_path: Optional[Path] = None
                         ) -> list[dict[str, Any]]:
    """Return latest Safety Gate versions in the customer's pilot view."""
    profile = load_profile(client_slug)
    selected: list[dict[str, Any]] = []
    with session(db_path) as conn:
        rows = conn.execute(
            "SELECT r.* FROM raw_item r "
            "WHERE r.source_kind = ? AND NOT EXISTS ("
            " SELECT 1 FROM raw_item newer WHERE newer.source_slug=r.source_slug "
            " AND newer.external_id=r.external_id AND newer.version>r.version) "
            "ORDER BY r.published_at, r.external_id",
            (SOURCE_KIND,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            reasons = client_match_reasons(payload, profile)
            if reasons:
                selected.append({
                    "raw_item_id": row["id"], "case_number": row["external_id"],
                    "title": row["title"], "published_at": row["published_at"],
                    "url": row["url"], "reasons": reasons,
                    "key_customers": tuple(
                        reason.removeprefix("online_trader:") for reason in reasons
                        if reason.startswith("online_trader:")),
                    "measures": parse_measures(payload.get("measures")),
                    "body_text": compose_body(payload),
                    "payload": payload,
                })
    return selected
