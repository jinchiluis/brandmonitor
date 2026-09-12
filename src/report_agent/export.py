"""Stage 1: freeze one client-week of stored material into a read-only bundle.

Deterministic, offline, and the only stage that touches the production
database. It opens SQLite through a ``mode=ro`` URI inside a transaction it
rolls back, so a report can be built while the pipeline is collecting and
cannot alter what it is reporting on.

What it freezes, and why each part is there:

``evidence.json``       every identity the assessor may report on
``stopped_items.json``  every identity a gate rejected, kept searchable because
                        an open issue must be able to find its continuation
                        among the gate's rejects (report_plan.md §2.3)
``gate_census.json``    every stored gate decision, so the accounting closes
``coverage.json``       what each source actually returned during the window,
                        including the sources that returned nothing
``manifest.json``       window, counts, stated policy, and SHA-256 of every
                        input file that shaped the selection

Eligibility is ``fetched_at`` before the cutoff on the latest version of each
identity - the windowing rule settled in todo.md §1.1. Publication and event
dates classify material for the narrative; they never silently discard it, and
per CLAUDE.md a ``lastmod`` date never becomes one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.collect import DEFAULT_NEWS_SOURCES, DEFAULT_REGULATORY_SOURCES
from src.db import DB_PATH
from src.logger import get_logger
from src.profile import ClientProfile, load_profile
from src.report_agent.bundle import Bundle, default_bundle_dir, window_bounds
from src.safety_gate import client_match_reasons, compose_body

logger = get_logger(__name__)

# published_at_source values that name a date the page or record asserted about
# itself. Everything else - notably 'lastmod' - leaves an item undated rather
# than dated wrongly; see CLAUDE.md, "Publication dates are not lastmod".
TRUSTED_DATE_SOURCES = frozenset(
    {"page", "feed", "news_sitemap", "record", "feed_resolved_from_discovery"})

POLICY = (
    "Raw-item eligibility uses fetched_at and the latest version before the cutoff.",
    "Event/publication dates classify eligible material for narrative; they do not "
    "silently discard undated evidence.",
    "A lastmod date never becomes a publication date; such items are undated here.",
    "Gate decisions available at export may route an item, but evidence fetched "
    "after the cutoff is excluded.",
    "Stopped items are retained for title/reason audit and for carry-forward "
    "search; a parliamentary 'unsure' does not pass.",
    "Successful title-gate bodies and the client-view Safety Gate supplement the "
    "gated rows; DSA aggregates are background only.",
    "No source network calls; no production assessment or report writes.",
)

HASHED_INPUTS = ("input/germany_medias.json", "input/regulatory_sources.json")


def _parliamentary_slugs(sources_path: Path = DEFAULT_REGULATORY_SOURCES) -> set[str]:
    """Sources stored by a collector rather than crawled: DIP, EP procedures.

    Read from the source list rather than hardcoded, so adding a structured
    collector does not need an edit here. Their gate 'unsure' is treated as a
    stop: a procedure the gate could not place is not a weekly finding.
    """
    entries = json.loads(Path(sources_path).read_text(encoding="utf-8"))
    slugs = set()
    for entry in entries:
        if entry.get("collector"):
            host = entry["url"].split("//", 1)[-1].split("/", 1)[0].lower()
            slugs.add(host.removeprefix("www."))
    return slugs


def _date_info(row: dict[str, Any], payload: dict[str, Any]) -> tuple[str | None, str]:
    """(day, provenance) for one stored row - the narrative date, or None.

    Each structured source is asked which of its dates is the event and which is
    the paperwork, because they are rarely the same field (CLAUDE.md).
    """
    provenance = payload.get("published_at_source")
    if row["source_kind"] == "safety_gate":
        return (payload.get("report") or {}).get("publication_date"), "weekly_bulletin"
    if row["source_kind"] == "dsa":
        # received_date is when a platform submitted, not when it acted.
        return payload.get("received_date"), "submission_date_not_event"
    if provenance == "discovery" and payload.get("discovered_via") in ("feeds", "feed", "rss"):
        provenance = "feed_resolved_from_discovery"
    if provenance in TRUSTED_DATE_SOURCES:
        return (row.get("published_at") or "")[:10] or None, provenance
    return None, provenance or "undated"


def _period(day: str | None, since: str, until: str) -> str:
    if not day:
        return "undated"
    if day < since:
        return "history"
    if day > until:
        return "future"
    return "week"


def export_window(client_slug: str, since: date, until: date, *,
                  out_dir: Path | None = None,
                  db_path: Path | None = None,
                  news_sources: Path = DEFAULT_NEWS_SOURCES,
                  regulatory_sources: Path = DEFAULT_REGULATORY_SOURCES) -> Bundle:
    """Freeze the window and return the bundle. Writes nothing to the database."""
    start, end = window_bounds(since, until)
    out = Path(out_dir) if out_dir else default_bundle_dir(client_slug, since, until)
    out.mkdir(parents=True, exist_ok=True)
    profile = load_profile(client_slug)
    parliamentary = _parliamentary_slugs(regulatory_sources)

    source = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        frozen = _collect(conn, profile, start, end, since, until, parliamentary)
    finally:
        conn.rollback()
        conn.close()

    for name, obj in frozen["files"].items():
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    for name, text in frozen["texts"].items():
        (out / name).write_text(text, encoding="utf-8")
    logger.info("[export] %s: %d candidates, %d stopped -> %s",
                client_slug, len(frozen["files"]["evidence.json"]),
                len(frozen["files"]["stopped_items.json"]), out)
    return Bundle(out)


def _collect(conn: sqlite3.Connection, profile: ClientProfile, start: str, end: str,
             since: date, until: date, parliamentary: set[str]) -> dict[str, Any]:
    def rows(query: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(query, tuple(args))]

    raw = rows("SELECT * FROM raw_item ORDER BY source_slug, external_id, version")
    # The latest version that existed at the cutoff. A restamp after the cutoff
    # must not change what the report saw.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw:
        if row["fetched_at"] < end:
            latest[(row["source_slug"], row["external_id"])] = row
    by_raw_id = {row["id"]: row for row in raw}

    assessments = rows("SELECT * FROM assessment WHERE client_slug = ? "
                       "ORDER BY created_at, id", (profile.slug,))
    gates: dict[tuple[str, str], dict[str, Any]] = {}
    for a in assessments:
        a["payload"] = json.loads(a["payload"])
        item = by_raw_id[a["raw_item_id"]]
        a["source_slug"], a["external_id"] = item["source_slug"], item["external_id"]
        # Later rows win: a re-gate under a newer prompt version supersedes.
        gates[(item["source_slug"], item["external_id"])] = a

    since_s, until_s = since.isoformat(), until.isoformat()

    def entry(row: dict[str, Any], route: str,
              gate: dict[str, Any] | None = None) -> dict[str, Any]:
        row = dict(row)
        payload = json.loads(row.pop("payload"))
        day, provenance = _date_info(row, payload)
        row.update(payload=payload, route=route, event_or_publication_day=day,
                   date_provenance=provenance, period=_period(day, since_s, until_s),
                   surfaced_in_window=start <= row["fetched_at"] < end)
        if gate:
            row["gate"] = gate
        if row["source_kind"] == "safety_gate":
            row["payload"]["body_text"] = compose_body(payload)
            row["client_reasons"] = list(client_match_reasons(payload, profile))
        return row

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    stopped: list[dict[str, Any]] = []
    after_cutoff: list[dict[str, Any]] = []
    for key, gate in gates.items():
        row = latest.get(key)
        if not row:
            # Gated on evidence that only arrived after the cutoff.
            after_cutoff.append(gate)
            continue
        unsure_passes = gate["relevant"] is None and key[0] not in parliamentary
        item = entry(row, "body_gate", gate)
        if gate["relevant"] == 1 or unsure_passes:
            candidates[key] = item
        else:
            stopped.append(item)

    # Title-gate routes: the cheap gate already made the relevance call, so a
    # body it caused to be fetched enters assessment without a body gate.
    title_routes: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for fetch in rows("SELECT * FROM body_fetch"):
        hint = json.loads(fetch.pop("hint_payload"))
        route = (hint.get("title_gate_routes") or {}).get(profile.slug)
        if not route or route.get("at", "") >= end:
            continue
        key = (fetch["source_slug"], fetch["external_id"])
        row = latest.get(key)
        title_routes.append({**fetch, "route": route,
                             "raw_item_id_asof": row["id"] if row else None})
        if not row:
            continue
        payload = json.loads(row["payload"])
        if payload.get("body_text") and payload.get("body_status") == "ok":
            item = entry(row, "title_gate_body")
            item["title_gate_route"] = route
            candidates[key] = item
        elif key not in candidates:
            # Routed for a body that never arrived. The report is allowed to say
            # a title looked promising and was unreadable; it may not guess at
            # what it said. Built through the same shaping as every other row so
            # it is a first-class identity: the ledger accounts for it, the
            # coverage page lists it, and the report may name it.
            item = entry(row, "unavailable_body")
            item["title_gate_route"] = route
            item["body_status"] = payload.get("body_status")
            item["fetch_status"] = fetch["status"]
            item["fetch_error"] = fetch.get("error")
            unavailable.append(item)

    for key, row in latest.items():
        if row["source_kind"] == "safety_gate":
            if client_match_reasons(json.loads(row["payload"]), profile):
                candidates[key] = entry(row, "safety_gate_client_view")
        elif row["source_kind"] == "dsa":
            candidates[key] = entry(row, "dsa_background")

    items = sorted(candidates.values(),
                   key=lambda r: (r["period"], r["source_kind"],
                                  r["event_or_publication_day"] or "", r["id"]))
    stopped.sort(key=lambda r: (r["period"], r["source_kind"],
                                r["event_or_publication_day"] or "", r["id"]))
    # Standing is frozen on the row rather than inferred from which file a row
    # came out of, so a tool result can say what an identity is without the
    # reader having to know how the export was assembled.
    for subset, standing in ((items, "candidate"), (stopped, "stopped"),
                             (unavailable, "unreadable")):
        for row in subset:
            row["in_export_as"] = standing

    runs = rows("SELECT * FROM run WHERE started_at >= ? AND started_at < ?", (start, end))
    run_kind = {r["id"]: r["kind"] for r in runs}
    run_sources = [r for r in rows("SELECT * FROM run_source") if r["run_id"] in run_kind]

    counts = Counter((r["source_kind"], r["route"], r["period"]) for r in items)
    manifest = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "client": profile.slug,
        "client_name": profile.name,
        "profile_version": profile.profile_version,
        "window_start": start,
        "window_end_exclusive": end,
        "display_window": f"{since.isoformat()} through {until.isoformat()} Europe/Berlin",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "database_raw_rows": len(raw),
        "assessment_rows": len(assessments),
        "unique_gated_identities": len(gates),
        "gated_identities_without_pre_cutoff_raw": len(after_cutoff),
        "stopped_asof_identities": len(stopped),
        "evidence_items": len(items),
        # What the ledger must account for: every identity this export froze,
        # unreadable titles included. The report quotes this number.
        "ledger_identities": len(items) + len(stopped) + len(unavailable),
        "candidate_and_stopped_identities": len(items) + len(stopped),
        "unavailable_title_routes": len(unavailable),
        "counts": [{"kind": k[0], "route": k[1], "period": k[2], "n": n}
                   for k, n in sorted(counts.items())],
        "parliamentary_sources": sorted(parliamentary),
        "policy": list(POLICY),
        "input_hashes": _input_hashes(profile.slug),
    }

    files = {
        "manifest.json": manifest,
        "evidence.json": items,
        "stopped_items.json": stopped,
        "gate_census.json": assessments,
        "post_cutoff_gate_items.json": after_cutoff,
        "title_routes.json": title_routes,
        "unavailable_title_evidence.json": unavailable,
        "dip_documents.json": rows(
            "SELECT * FROM dip_document WHERE fetched_at IS NULL OR fetched_at < ?", (end,)),
        "collection_runs.json": runs,
        "source_runs.json": run_sources,
        "coverage.json": _coverage(run_sources, run_kind),
        "client_profile.json": _client_file(profile.slug, "profile.json"),
    }
    taxonomy = _client_file(profile.slug, "alert_taxonomy.json")
    if taxonomy is not None:
        files["client_alert_taxonomy.json"] = taxonomy

    texts = {"candidate_index.md": _index(items, "Frozen candidate index"),
             "stopped_index.md": _index(stopped, "Frozen stopped-item index")}
    return {"files": files, "texts": texts}


def _coverage(run_sources: list[dict[str, Any]], run_kind: dict[int, str]) -> list[dict]:
    """Per-source outcome over the window, zero-yield sources included.

    A source that looked and found nothing is a reportable fact, not an absence
    in a table - the report has to be able to say a source produced nothing.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in run_sources:
        grouped[(run_kind[row["run_id"]], row["source_slug"])].append(row)
    coverage = []
    for (kind, slug), entries in sorted(grouped.items()):
        entries.sort(key=lambda r: r["run_id"])
        last = entries[-1]
        coverage.append({
            "kind": kind, "source": slug, "runs": len(entries),
            "statuses": dict(sorted(Counter(r["status"] for r in entries).items())),
            "items_found_sum": sum(r["items_found"] or 0 for r in entries),
            "items_stored_sum": sum(r["items_stored"] or 0 for r in entries),
            "latest_status": last["status"], "latest_found": last["items_found"],
            "latest_error": last["error"],
        })
    return coverage


def _client_file(client_slug: str, name: str) -> Any:
    from src.profile import CLIENTS_DIR
    path = CLIENTS_DIR / client_slug / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _input_hashes(client_slug: str) -> dict[str, str]:
    from src.config import ROOT
    paths = [f"clients/{client_slug}/profile.json",
             f"clients/{client_slug}/alert_taxonomy.json", *HASHED_INPUTS]
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in paths if (ROOT / p).exists()}


def _index(subset: list[dict[str, Any]], heading: str) -> str:
    lines = [f"# {heading}", "",
             "| ID | Period | Kind / route | Date / provenance | Source | Title | Gate reason |",
             "|---|---|---|---|---|---|---|"]
    for row in subset:
        if row["source_kind"] == "dsa":
            continue
        reason = ((row.get("gate") or {}).get("payload") or {}).get("reason", "")
        cells = [row["id"], row["period"], f"{row['source_kind']} / {row['route']}",
                 f"{row['event_or_publication_day']} / {row['date_provenance']}",
                 row["source_slug"], row["title"], reason]
        lines.append("| " + " | ".join(
            str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |")
    return "\n".join(lines) + "\n"
