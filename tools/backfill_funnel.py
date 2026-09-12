#!/usr/bin/env python3
"""Manual, resumable backfill of the client selection/gating funnel.

This file is deliberately outside ``run.py`` and is never called by
``run_daily.bat``.  Use ``tools/backfill_funnel.bat`` on Windows so the campaign
holds the same lock as the daily pipeline.

A campaign freezes three sets under the current client profile:

* news bodies selected for the news body gate;
* regulatory bodies selected for the regulatory body gate;
* title-only news candidates which still need a title decision and body fetch.

Title decisions are written below the campaign directory, not below the live
``data/title_gate`` handoff. A successfully fetched title keep goes directly to
the full assessor; it does not receive a second cheap body-gate decision. Body-gate
verdicts for the other two lanes belong in ``assessment`` because they are already
client-specific there; shared ``raw_item`` rows are never marked relevant or
irrelevant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bodies import queue_body, run_body_fetch  # noqa: E402
from src.collect import DEFAULT_NEWS_SOURCES, DEFAULT_REGULATORY_SOURCES  # noqa: E402
from src.config import (BODY_FETCH_LIMIT, BODY_GATE_BODY_CHARS, BODY_GATE_LIMIT,  # noqa: E402
                        BODY_GATE_MODEL, BODY_GATE_REASONING, BODY_GATE_WORKERS,
                        DATA_DIR, TITLE_GATE_BATCH_SIZE, TITLE_GATE_MODEL,
                        TITLE_GATE_REASONING)
from src.db import DB_PATH, session, utcnow  # noqa: E402
from src.profile import ClientProfile, load_profile  # noqa: E402
from src.selector import Candidate  # noqa: E402
from src.title_gate import (GateConfigError, gate_candidates,  # noqa: E402
                            openai_caller as title_caller,
                            prompt_version as title_prompt_version,
                            render_system_prompt as title_system, run_gate)
from src.body_gate import (_decide as decide_body, GateItem,  # noqa: E402
                           openai_caller as body_caller, pending_items,
                           prompt_version as body_prompt_version,
                           render_system_prompt as body_system, run_body_gate)


SCHEMA_VERSION = 1
DEFAULT_CAMPAIGN_ROOT = DATA_DIR / "backfill" / "funnel"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
LOCK_ENV = "BRANDMONITOR_BACKFILL_LOCKED"


class CampaignError(RuntimeError):
    """The campaign is missing, inconsistent, or unsafe to continue."""


def campaign_path(root: Path, name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise CampaignError("campaign names may contain only letters, numbers, '.', '_' and '-'")
    return Path(root) / name


def _json_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise CampaignError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(row, dict):
            raise CampaignError(f"invalid JSONL object at {path}:{number}")
        rows.append(row)
    return rows


def _candidate_record(candidate: Candidate, *, raw_item_id: int | None = None) -> dict[str, Any]:
    row = {
        "source": candidate.source_slug,
        "source_kind": candidate.source_kind,
        "external_id": candidate.external_id,
        "url": candidate.url,
        "title": candidate.title,
        "published_at": candidate.published_at,
        "reasons": list(candidate.reasons),
        "matched_in": list(candidate.matched_in),
        "has_body": candidate.has_body,
    }
    if raw_item_id is not None:
        row["raw_item_id"] = raw_item_id
    return row


def _candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        source_slug=row["source"], source_kind=row.get("source_kind", "news"),
        external_id=row["external_id"], url=row["url"], title=row.get("title"),
        published_at=row.get("published_at"), reasons=tuple(row.get("reasons") or ()),
        matched_in=tuple(row.get("matched_in") or ()), has_body=bool(row.get("has_body")),
    )


def _versions(profile: ClientProfile) -> dict[str, Any]:
    title_prompt = title_prompt_version(title_system(profile))
    bodies = {
        kind: body_prompt_version(kind, body_system(profile, kind))
        for kind in ("news", "regulatory")
    }
    return {
        "profile": profile.profile_version,
        "title_prompt": title_prompt,
        "body_prompts": bodies,
        "title_model": TITLE_GATE_MODEL,
        "title_reasoning": TITLE_GATE_REASONING,
        "body_model": BODY_GATE_MODEL,
        "body_reasoning": BODY_GATE_REASONING,
    }


def create_campaign(name: str, *, client: str | Path = "jt-express",
                    db_path: Path = DB_PATH, news_sources: Path = DEFAULT_NEWS_SOURCES,
                    regulatory_sources: Path = DEFAULT_REGULATORY_SOURCES,
                    root: Path = DEFAULT_CAMPAIGN_ROOT) -> dict[str, Any]:
    """Freeze the current pending funnel without calling a model or fetching a URL."""
    directory = campaign_path(root, name)
    if directory.exists():
        raise CampaignError(f"campaign already exists: {directory}")
    db_path = Path(db_path).resolve()
    news_sources = Path(news_sources).resolve()
    regulatory_sources = Path(regulatory_sources).resolve()
    profile = load_profile(client)
    client_path = Path(client)
    profile_source = (str(client_path.resolve()) if client_path.suffix else profile.slug)

    # Render every prompt before creating anything. A profile that cannot run one
    # tier must not leave behind a campaign that looks usable.
    versions = _versions(profile)
    news_items, news_already = pending_items(
        profile, "news", db_path, news_sources)
    regulatory_items, regulatory_already = pending_items(
        profile, "regulatory", db_path, regulatory_sources)
    title_items, title_with_body = gate_candidates(profile, db_path, news_sources, None)

    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
        cutoff = conn.execute("SELECT COALESCE(MAX(id), 0) FROM raw_item").fetchone()[0]

    directory.mkdir(parents=True)
    _write_jsonl(directory / "body-news.jsonl", (
        _candidate_record(item.candidate, raw_item_id=item.raw_item_id)
        for item in news_items
    ))
    _write_jsonl(directory / "body-regulatory.jsonl", (
        _candidate_record(item.candidate, raw_item_id=item.raw_item_id)
        for item in regulatory_items
    ))
    _write_jsonl(directory / "title-pending.jsonl", (
        _candidate_record(item) for item in title_items
    ))
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "campaign": name,
        "created_at": utcnow(),
        "client": profile.slug,
        "profile_source": profile_source,
        "database": str(db_path),
        "cutoff_raw_item_id": cutoff,
        "sources": {
            "news": str(news_sources), "news_sha256": _json_hash(news_sources),
            "regulatory": str(regulatory_sources),
            "regulatory_sha256": _json_hash(regulatory_sources),
        },
        "versions": versions,
        "counts": {
            "body_news_pending": len(news_items),
            "body_news_already_gated": news_already,
            "body_regulatory_pending": len(regulatory_items),
            "body_regulatory_already_gated": regulatory_already,
            "title_pending": len(title_items),
            "title_with_body": len(title_with_body),
        },
    }
    _write_json(directory / "campaign.json", metadata)
    return metadata


def load_campaign(name: str, *, root: Path = DEFAULT_CAMPAIGN_ROOT,
                  db_path: Path | None = None,
                  validate_sources: bool = True) -> tuple[Path, dict[str, Any], ClientProfile]:
    directory = campaign_path(root, name)
    metadata_path = directory / "campaign.json"
    if not metadata_path.exists():
        raise CampaignError(f"campaign does not exist: {directory}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise CampaignError(f"unsupported campaign schema: {metadata.get('schema_version')}")
    actual_db = Path(db_path or metadata["database"]).resolve()
    if actual_db != Path(metadata["database"]).resolve():
        raise CampaignError(
            f"campaign belongs to {metadata['database']}, not {actual_db}")
    if validate_sources:
        for kind in ("news", "regulatory"):
            source_path = Path(metadata["sources"][kind])
            expected_hash = metadata["sources"][f"{kind}_sha256"]
            if not source_path.exists() or _json_hash(source_path) != expected_hash:
                raise CampaignError(
                    f"{kind} source configuration changed after this campaign was frozen; "
                    "create a new campaign")
    profile = load_profile(metadata.get("profile_source", metadata["client"]))
    current = _versions(profile)
    if current != metadata.get("versions"):
        raise CampaignError(
            "client profile, prompt, model, or reasoning changed after this campaign was frozen; "
            "create a new campaign")
    return directory, metadata, profile


def _title_decisions(directory: Path, client: str) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    decision_root = directory / "title-decisions" / client
    for path in sorted(decision_root.glob("*.jsonl")):
        for row in _read_jsonl(path):
            if row.get("client") != client:
                raise CampaignError(f"wrong client in {path}: {row.get('client')!r}")
            key = (row.get("source"), row.get("external_id"))
            if not all(key) or row.get("decision") not in {"keep", "drop"}:
                raise CampaignError(f"invalid title decision in {path}")
            latest[key] = row
    return latest


def decide_titles(name: str, *, limit: int = TITLE_GATE_BATCH_SIZE,
                  root: Path = DEFAULT_CAMPAIGN_ROOT, db_path: Path | None = None,
                  caller=None) -> dict[str, Any]:
    directory, metadata, profile = load_campaign(name, root=root, db_path=db_path)
    rows = _read_jsonl(directory / "title-pending.jsonl")
    decided = _title_decisions(directory, profile.slug)
    pending = [row for row in rows if (row["source"], row["external_id"]) not in decided]
    batch = [_candidate(row) for row in pending[:limit]]
    if not batch:
        return {
            "attempted": 0, "remaining": 0, "kept": 0, "dropped": 0,
            "fail_open_batches": 0,
            "prompt_version": metadata["versions"]["title_prompt"],
            "input_tokens": 0, "output_tokens": 0,
        }
    result = run_gate(
        batch, profile, caller or title_caller(), batch_size=TITLE_GATE_BATCH_SIZE,
        log_root=directory / "title-decisions", keep_days=36500)
    return {
        "attempted": len(batch), "remaining": len(pending) - len(batch),
        "kept": len(result.kept), "dropped": len(result.dropped),
        "fail_open_batches": result.failed_batches,
        "prompt_version": metadata["versions"]["title_prompt"],
        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
    }


def _body_gated_keys(conn: sqlite3.Connection, client: str, kind: str) -> set[tuple[str, str]]:
    prefix = f"body_gate-{kind}-%"
    return {(row["source_slug"], row["external_id"]) for row in conn.execute(
        "SELECT DISTINCT r.source_slug, r.external_id FROM assessment a "
        "JOIN raw_item r ON r.id=a.raw_item_id WHERE a.client_slug=? "
        "AND r.source_kind=? AND a.prompt_version LIKE ?",
        (client, kind, prefix))}


def _manifest_gate_items(directory: Path, filename: str, db_path: Path,
                         client: str, kind: str) -> list[GateItem]:
    rows = _read_jsonl(directory / filename)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        gated = _body_gated_keys(conn, client, kind)
        items: list[GateItem] = []
        for record in rows:
            key = (record["source"], record["external_id"])
            if key in gated:
                continue
            raw = conn.execute(
                "SELECT source_slug, external_id, source_kind, payload FROM raw_item WHERE id=?",
                               (record["raw_item_id"],)).fetchone()
            if raw is None:
                raise CampaignError(f"raw item {record['raw_item_id']} was removed")
            if ((raw["source_slug"], raw["external_id"], raw["source_kind"])
                    != (record["source"], record["external_id"], kind)):
                raise CampaignError(
                    f"raw item {record['raw_item_id']} no longer matches the frozen campaign")
            try:
                body = json.loads(raw["payload"]).get("body_text") or ""
            except (TypeError, ValueError) as exc:
                raise CampaignError(f"raw item {record['raw_item_id']} has invalid payload") from exc
            if not body:
                raise CampaignError(f"raw item {record['raw_item_id']} no longer has a body")
            items.append(GateItem(_candidate(record), record["raw_item_id"], body))
        return items
    finally:
        conn.close()


def gate_existing_bodies(name: str, kind: str, *, limit: int = BODY_GATE_LIMIT,
                         root: Path = DEFAULT_CAMPAIGN_ROOT, db_path: Path | None = None,
                         caller=None, workers: int | None = None) -> dict[str, Any]:
    if kind not in {"news", "regulatory"}:
        raise CampaignError("existing body kind must be news or regulatory")
    directory, metadata, profile = load_campaign(name, root=root, db_path=db_path)
    actual_db = Path(metadata["database"])
    items = _manifest_gate_items(directory, f"body-{kind}.jsonl", actual_db,
                                 profile.slug, kind)
    batch = items[:limit]
    if not batch:
        return {
            "attempted": 0, "remaining": 0, "relevant": 0, "unsure": 0,
            "irrelevant": 0, "fail_open": 0, "stopped": None,
            "prompt_version": metadata["versions"]["body_prompts"][kind],
            "input_tokens": 0, "output_tokens": 0,
        }
    kwargs = {"db_path": actual_db}
    if workers is not None:
        kwargs["workers"] = workers
    result = run_body_gate(batch, profile, kind, caller or body_caller(), **kwargs)
    return {
        "attempted": len(result.decisions), "remaining": len(items) - len(result.decisions),
        "relevant": result.count("relevant"), "unsure": result.count("unsure"),
        "irrelevant": result.count("irrelevant"), "fail_open": result.fail_open,
        "stopped": result.stopped, "prompt_version": result.prompt_version,
        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
    }


def promote_title_keeps(name: str, *, limit: int,
                        root: Path = DEFAULT_CAMPAIGN_ROOT,
                        db_path: Path | None = None) -> dict[str, int]:
    """Put at most ``limit`` staged keeps into the durable body queue."""
    directory, metadata, profile = load_campaign(name, root=root, db_path=db_path)
    actual_db = Path(metadata["database"])
    decisions = _title_decisions(directory, profile.slug)
    planned = len(_read_jsonl(directory / "title-pending.jsonl"))
    if len(decisions) != planned:
        raise CampaignError(
            f"title review is incomplete: {len(decisions)} of {planned} decisions exist")
    keeps = [row for row in decisions.values() if row["decision"] == "keep"]
    promoted = 0
    already = 0
    with session(actual_db) as conn:
        for decision in keeps:
            task = conn.execute(
                "SELECT hint_payload FROM body_fetch WHERE source_slug=? AND external_id=?",
                (decision["source"], decision["external_id"])).fetchone()
            routes = {}
            if task:
                try:
                    routes = json.loads(task["hint_payload"]).get("title_gate_routes", {})
                except (TypeError, ValueError):
                    routes = {}
            existing = routes.get(profile.slug) or {}
            if existing.get("backfill_campaign") == name:
                already += 1
                continue
            if promoted >= limit:
                continue
            raw = conn.execute(
                "SELECT * FROM raw_item WHERE source_slug=? AND external_id=? "
                "AND source_kind='news' ORDER BY version DESC LIMIT 1",
                (decision["source"], decision["external_id"])).fetchone()
            if raw is None:
                raise CampaignError(
                    f"title keep has no raw item: {decision['source']} {decision['external_id']}")
            route = {
                "client": profile.slug, "at": decision.get("at"),
                "prompt_version": decision.get("prompt_version"),
                "profile_version": decision.get("profile_version"),
                "reasons": decision.get("reasons", []),
                "matched_in": decision.get("matched_in", ["title_gate"]),
                "label": decision.get("label"), "backfill_campaign": name,
            }
            payload = json.loads(raw["payload"])
            queue_body(conn, raw["source_slug"], raw["external_id"], payload,
                       only_missing=True, title_gate_route=route)
            if payload.get("body_text"):
                # The body arrived by another route after the campaign snapshot.
                conn.execute(
                    "UPDATE body_fetch SET status='ok', error=NULL WHERE source_slug=? "
                    "AND external_id=?", (raw["source_slug"], raw["external_id"]))
            promoted += 1
    return {"promoted": promoted, "already_promoted": already,
            "keeps": len(keeps), "remaining": len(keeps) - already - promoted}


def fetch_title_keeps(name: str, *, limit: int = BODY_FETCH_LIMIT,
                      root: Path = DEFAULT_CAMPAIGN_ROOT,
                      db_path: Path | None = None) -> dict[str, Any]:
    directory, metadata, _profile = load_campaign(name, root=root, db_path=db_path)
    promotion = promote_title_keeps(name, limit=limit, root=root, db_path=db_path)
    summary = run_body_fetch(
        Path(metadata["sources"]["news"]), kind="news", limit=limit,
        db_path=Path(metadata["database"]), title_gate_client=metadata["client"],
        # Deliberately empty: staged decisions must never be bulk-ingested by
        # production's retained-JSONL reader.
        title_gate_log_root=directory / "empty-live-title-log",
    )
    summary["promotion"] = promotion
    return summary


SHADOW_FILENAME = "title-body-gate-shadow.jsonl"


def _shadow_rows(directory: Path) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    path = directory / SHADOW_FILENAME
    for row in _read_jsonl(path):
        key = (row.get("source"), row.get("external_id"))
        if not all(key) or row.get("verdict") not in {"relevant", "unsure", "irrelevant"}:
            raise CampaignError(f"invalid shadow body-gate decision in {path}")
        latest[key] = row
    return latest


def _fetched_title_keep_items(directory: Path, metadata: dict[str, Any],
                              profile: ClientProfile) -> tuple[list[GateItem], int]:
    """Return fetched campaign keeps without making them body-gate candidates."""
    decisions = _title_decisions(directory, profile.slug)
    keeps = [row for row in decisions.values() if row["decision"] == "keep"]
    db_path = Path(metadata["database"])
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    items: list[GateItem] = []
    unavailable = 0
    try:
        for decision in keeps:
            key = (decision["source"], decision["external_id"])
            task = conn.execute(
                "SELECT status, hint_payload FROM body_fetch WHERE source_slug=? "
                "AND external_id=?", key).fetchone()
            route = {}
            if task:
                try:
                    route = json.loads(task["hint_payload"]).get(
                        "title_gate_routes", {}).get(profile.slug) or {}
                except (TypeError, ValueError):
                    route = {}
            if (task is None or task["status"] != "ok" or
                    route.get("backfill_campaign") != metadata["campaign"]):
                unavailable += 1
                continue
            raw = conn.execute(
                "SELECT * FROM raw_item WHERE source_slug=? AND external_id=? "
                "AND source_kind='news' ORDER BY version DESC LIMIT 1", key).fetchone()
            if raw is None:
                unavailable += 1
                continue
            try:
                body = json.loads(raw["payload"]).get("body_text") or ""
            except (TypeError, ValueError):
                body = ""
            if not body:
                unavailable += 1
                continue
            candidate = Candidate(
                source_slug=raw["source_slug"], source_kind="news",
                external_id=raw["external_id"], url=raw["url"], title=raw["title"],
                published_at=raw["published_at"],
                reasons=tuple(decision.get("reasons") or ()),
                matched_in=tuple(decision.get("matched_in") or ()), has_body=True,
            )
            items.append(GateItem(candidate, raw["id"], body))
    finally:
        conn.close()
    return items, unavailable


def shadow_gate_title_keeps(name: str, *, limit: int = BODY_GATE_LIMIT,
                            root: Path = DEFAULT_CAMPAIGN_ROOT,
                            db_path: Path | None = None, caller=None,
                            workers: int = BODY_GATE_WORKERS) -> dict[str, Any]:
    """Measure the body gate on fetched title keeps without changing their route.

    Results live only in the campaign JSONL. They are never inserted into
    ``assessment`` and therefore cannot suppress a title keep from the future full
    assessor.
    """
    # Source discovery/fetch configuration is immaterial here: this evaluates the
    # campaign's frozen title decisions against bodies that are already stored.
    # Profile, prompt, model and database identity remain frozen and validated.
    directory, metadata, profile = load_campaign(
        name, root=root, db_path=db_path, validate_sources=False)
    items, unavailable = _fetched_title_keep_items(directory, metadata, profile)
    existing = _shadow_rows(directory)
    pending = [item for item in items if (
        item.candidate.source_slug, item.candidate.external_id) not in existing]
    batch = pending[:limit]
    system = body_system(profile, "news")
    version = body_prompt_version("news", system)
    model_call = caller or body_caller()
    decisions = []
    if batch:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            decisions = list(pool.map(
                lambda item: decide_body(
                    system, item, "news", model_call, BODY_GATE_BODY_CHARS), batch))
        path = directory / SHADOW_FILENAME
        with path.open("a", encoding="utf-8") as handle:
            for decision in decisions:
                candidate = decision.item.candidate
                row = {
                    "at": utcnow(), "campaign": name, "client": profile.slug,
                    "profile_version": profile.profile_version,
                    "prompt_version": version, "model": BODY_GATE_MODEL,
                    "reasoning_effort": BODY_GATE_REASONING,
                    "source": candidate.source_slug,
                    "external_id": candidate.external_id,
                    "raw_item_id": decision.item.raw_item_id,
                    "title": candidate.title, "published_at": candidate.published_at,
                    "selector_reasons": list(candidate.reasons),
                    "verdict": decision.verdict, "reason": decision.reason,
                    "fail_open": decision.fail_open, "error": decision.error,
                    "tokens": {"in": decision.input_tokens, "out": decision.output_tokens},
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    cumulative = _shadow_rows(directory)
    return {
        "attempted": len(decisions), "remaining": len(pending) - len(decisions),
        "available_bodies": len(items), "unavailable_keeps": unavailable,
        "this_run": {
            verdict: sum(d.verdict == verdict for d in decisions)
            for verdict in ("relevant", "unsure", "irrelevant")
        },
        "cumulative": {
            verdict: sum(row["verdict"] == verdict for row in cumulative.values())
            for verdict in ("relevant", "unsure", "irrelevant")
        },
        "fail_open": sum(d.fail_open for d in decisions),
        "prompt_version": version,
        "input_tokens": sum(d.input_tokens for d in decisions),
        "output_tokens": sum(d.output_tokens for d in decisions),
        "output": str(directory / SHADOW_FILENAME),
        "routing_changed": False,
    }


def _title_keep_progress(directory: Path, metadata: dict[str, Any],
                         profile: ClientProfile) -> dict[str, int]:
    """Count staged keeps as promoted, fetched and ready for full assessment."""
    decisions = _title_decisions(directory, profile.slug)
    keep_rows = {(row["source"], row["external_id"]): row for row in decisions.values()
                 if row["decision"] == "keep"}
    db_path = Path(metadata["database"])
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        promoted = fetched = ready = 0
        for key in keep_rows:
            task = conn.execute(
                "SELECT status, hint_payload FROM body_fetch WHERE source_slug=? "
                "AND external_id=?", key).fetchone()
            if task is None:
                continue
            try:
                route = json.loads(task["hint_payload"]).get(
                    "title_gate_routes", {}).get(profile.slug) or {}
            except (TypeError, ValueError):
                route = {}
            if route.get("backfill_campaign") != metadata["campaign"]:
                continue
            promoted += 1
            if task["status"] != "ok":
                continue
            fetched += 1
            raw = conn.execute(
                "SELECT * FROM raw_item WHERE source_slug=? AND external_id=? "
                "AND source_kind='news' ORDER BY version DESC LIMIT 1", key).fetchone()
            if raw is None:
                continue
            try:
                body = json.loads(raw["payload"]).get("body_text") or ""
            except (TypeError, ValueError):
                body = ""
            if body:
                ready += 1
        return {"keeps": len(keep_rows), "promoted": promoted,
                "fetched": fetched, "ready_for_assessor": ready}
    finally:
        conn.close()


def campaign_status(name: str, *, root: Path = DEFAULT_CAMPAIGN_ROOT,
                    db_path: Path | None = None) -> dict[str, Any]:
    directory, metadata, profile = load_campaign(name, root=root, db_path=db_path)
    decisions = _title_decisions(directory, profile.slug)
    result: dict[str, Any] = {
        "campaign": name, "client": profile.slug, "cutoff_raw_item_id":
        metadata["cutoff_raw_item_id"], "planned": metadata["counts"],
        "titles": {
            "decided": len(decisions),
            "keep": sum(row["decision"] == "keep" for row in decisions.values()),
            "drop": sum(row["decision"] == "drop" for row in decisions.values()),
            "fail_open": sum(bool(row.get("fail_open")) for row in decisions.values()),
        },
    }
    db = Path(metadata["database"])
    result["body_gate_remaining"] = {
        kind: len(_manifest_gate_items(directory, f"body-{kind}.jsonl", db,
                                       profile.slug, kind))
        for kind in ("news", "regulatory")
    }
    result["title_keeps"] = _title_keep_progress(directory, metadata, profile)
    shadow = _shadow_rows(directory)
    result["title_body_gate_shadow"] = {
        "decided": len(shadow),
        "relevant": sum(row["verdict"] == "relevant" for row in shadow.values()),
        "unsure": sum(row["verdict"] == "unsure" for row in shadow.values()),
        "irrelevant": sum(row["verdict"] == "irrelevant" for row in shadow.values()),
        "fail_open": sum(bool(row.get("fail_open")) for row in shadow.values()),
    }
    return result


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _common(parser: argparse.ArgumentParser, *, include_client: bool = False) -> None:
    parser.add_argument("campaign")
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN_ROOT)
    parser.add_argument("--db", type=Path, default=None)
    if include_client:
        parser.add_argument("--client", default="jt-express")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual, isolated backfill of title and body gates")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="freeze a read-only campaign manifest")
    _common(create, include_client=True)
    create.add_argument("--news-sources", type=Path, default=DEFAULT_NEWS_SOURCES)
    create.add_argument("--regulatory-sources", type=Path,
                        default=DEFAULT_REGULATORY_SOURCES)

    status = sub.add_parser("status", help="show campaign progress without changing it")
    _common(status)

    titles = sub.add_parser("title-gate", help="decide a bounded title-only batch")
    _common(titles)
    titles.add_argument("--limit", type=_positive, default=TITLE_GATE_BATCH_SIZE)

    bodies = sub.add_parser("body-gate-existing", help="gate frozen existing bodies")
    _common(bodies)
    bodies.add_argument("--kind", choices=("news", "regulatory"), required=True)
    bodies.add_argument("--limit", type=_positive, default=BODY_GATE_LIMIT)

    fetch = sub.add_parser("fetch-title-keeps",
                           help="promote and fetch a bounded staged keep batch")
    _common(fetch)
    fetch.add_argument("--limit", type=_positive, default=BODY_FETCH_LIMIT)

    shadow = sub.add_parser(
        "shadow-body-gate-title-keeps",
        help="measure the body gate on fetched title keeps without changing routing")
    _common(shadow)
    shadow.add_argument("--limit", type=_positive, default=BODY_GATE_LIMIT)

    return parser


def _require_wrapper(command: str) -> None:
    if command != "status" and os.environ.get(LOCK_ENV) != "1":
        raise CampaignError(
            "mutating campaign commands must run through tools\\backfill_funnel.bat "
            "so they cannot overlap the daily pipeline")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _require_wrapper(args.command)
        if args.command == "create":
            result = create_campaign(
                args.campaign, client=args.client, db_path=args.db or DB_PATH,
                news_sources=args.news_sources, regulatory_sources=args.regulatory_sources,
                root=args.campaign_root)
        elif args.command == "status":
            result = campaign_status(args.campaign, root=args.campaign_root, db_path=args.db)
        elif args.command == "title-gate":
            result = decide_titles(args.campaign, limit=args.limit,
                                   root=args.campaign_root, db_path=args.db)
        elif args.command == "body-gate-existing":
            result = gate_existing_bodies(args.campaign, args.kind, limit=args.limit,
                                          root=args.campaign_root, db_path=args.db)
        elif args.command == "fetch-title-keeps":
            result = fetch_title_keeps(args.campaign, limit=args.limit,
                                       root=args.campaign_root, db_path=args.db)
        elif args.command == "shadow-body-gate-title-keeps":
            result = shadow_gate_title_keeps(
                args.campaign, limit=args.limit,
                root=args.campaign_root, db_path=args.db)
        else:
            raise CampaignError(f"unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CampaignError, GateConfigError, FileNotFoundError, ValueError) as exc:
        print(f"backfill error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
