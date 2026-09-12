#!/usr/bin/env python3
"""Build a read-only daily coverage-health verdict from the production database.

The collector remains the authority for storing source material. This observer
only reads its durable run accounting and the current canary snapshot, then writes
small immutable health snapshots outside SQLite. A warning/critical finding is a
successful analysis and therefore exits zero; exit 2 means the observer broke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from health.common import ROOT, format_utc, now_utc, publish_snapshot  # noqa: E402


DEFAULT_DB = ROOT / "data" / "brandmonitor.sqlite3"
DEFAULT_NEWS_SOURCES = ROOT / "input" / "germany_medias.json"
DEFAULT_REGULATORY_SOURCES = ROOT / "input" / "regulatory_sources.json"
DEFAULT_CANARY_CONFIG = ROOT / "health" / "canaries.json"
DEFAULT_CANARY = ROOT / "data" / "health" / "canaries" / "latest.json"
DEFAULT_OUTPUT = ROOT / "data" / "health"
UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
RULES_VERSION = "coverage-health-v1"
SEVERITY_RANK = {"healthy": 0, "learning": 0, "warning": 1, "critical": 2}
BASELINE_RUNS = 7
BASELINE_MAX_RUNS = 28
COMPARABLE_WINDOW = timedelta(hours=36)


class AnalysisError(RuntimeError):
    pass


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _slug(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _source_entries(path: Path, *, include_collectors: bool = False) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise AnalysisError(f"source file {path} must contain a JSON array")
    entries = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("url"):
            raise AnalysisError(f"source file {path} contains an entry without url")
        if entry.get("collector") and not include_collectors:
            continue
        entries.append(entry)
    return entries


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _window_is_comparable(start: str | None, end: str | None) -> bool:
    beginning, finish = _parse_time(start), _parse_time(end)
    return bool(beginning and finish and timedelta(0) <= finish - beginning <= COMPARABLE_WINDOW)


def _latest_per_berlin_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse manual reruns so one busy test day cannot train a daily baseline."""
    daily: dict[str, dict[str, Any]] = {}
    for row in rows:
        started = _parse_time(row.get("started_at"))
        if started is None:
            continue
        day = started.astimezone(BERLIN).date().isoformat()
        daily[day] = row  # rows arrive by run id, so the last observation wins
    return [daily[day] for day in sorted(daily)]


def _incident(source: str, check: str, severity: str, message: str) -> dict[str, str]:
    return {"source": source, "check": check, "severity": severity, "message": message}


def _load_canary_sources(path: Path) -> tuple[set[str], bytes]:
    raw_bytes = path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    checks = raw.get("checks", []) if isinstance(raw, dict) else []
    critical = {
        check["source_slug"]
        for check in checks
        if isinstance(check, dict)
        and check.get("failure_severity") == "critical"
        and isinstance(check.get("source_slug"), str)
    }
    return critical, raw_bytes


def _latest_runs(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT * FROM run WHERE finished_at IS NOT NULL ORDER BY id"
    ):
        latest[row["kind"]] = dict(row)
    return latest


def _run_source_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT rs.*, r.kind, r.started_at, r.finished_at, r.window_start, "
            "r.window_end, r.status AS run_status "
            "FROM run_source rs JOIN run r ON r.id = rs.run_id "
            "WHERE r.finished_at IS NOT NULL ORDER BY r.id"
        )
    ]


def _source_metrics(
    *, expected: dict[str, list[dict[str, Any]]], latest_runs: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]], watermarks: dict[str, dict[str, Any]],
    critical_sources: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_run: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["kind"], row["source_slug"])
        by_key[key].append(row)
        by_run[(row["run_id"], row["source_slug"])] = row

    metrics: list[dict[str, Any]] = []
    incidents: list[dict[str, str]] = []
    learning = 0
    for kind, entries in expected.items():
        latest_run = latest_runs.get(kind)
        for entry in entries:
            slug = _slug(entry["url"])
            history = by_key.get((kind, slug), [])
            current = by_run.get((latest_run["id"], slug)) if latest_run else None
            daily_history = _latest_per_berlin_day(history)
            recent = daily_history[-7:]
            statuses = [row["status"] for row in recent]
            zero_streak = 0
            failure_streak = 0
            for status in reversed(statuses):
                if status == "zero":
                    zero_streak += 1
                else:
                    break
            for status in reversed(statuses):
                if status == "failed":
                    failure_streak += 1
                else:
                    break

            comparable = [
                row for row in daily_history
                if row["status"] in {"ok", "zero"}
                and _window_is_comparable(row["window_start"], row["window_end"])
            ][-BASELINE_MAX_RUNS:]
            previous = [row["items_found"] for row in comparable if not current or row["run_id"] != current["run_id"]]
            baseline_ready = len(previous) >= BASELINE_RUNS
            baseline = float(statistics.median(previous)) if baseline_ready else None
            if not baseline_ready:
                learning += 1

            severity = "critical" if slug in critical_sources else "warning"
            if latest_run is None:
                incidents.append(_incident(
                    slug, "missing_collection_run", "critical",
                    f"no completed {kind} collection run exists",
                ))
            elif current is None:
                incidents.append(_incident(
                    slug, "missing_source_result", "critical",
                    f"source has no result in latest {kind} run {latest_run['id']}",
                ))
            elif current["status"] == "failed":
                incidents.append(_incident(
                    slug, "source_failed", severity,
                    f"latest {kind} run failed for this source: {current['error'] or 'no error recorded'}",
                ))
            elif current["status"] in {"ok", "zero"}:
                scope = f"collection:{kind}:{slug}"
                mark = watermarks.get(scope)
                mark_position = _parse_time(mark["position"]) if mark else None
                expected_position = _parse_time(latest_run["window_end"])
                if not mark_position or not expected_position or abs(
                    (mark_position - expected_position).total_seconds()
                ) > 60:
                    incidents.append(_incident(
                        slug, "watermark_not_at_latest_run", "critical",
                        f"successful source result is not checkpointed at run {latest_run['id']} window end",
                    ))

                if baseline_ready and baseline is not None and baseline >= 2:
                    if zero_streak >= 2:
                        incidents.append(_incident(
                            slug, "zero_streak", severity,
                            f"source returned zero for {zero_streak} consecutive runs; "
                            f"prior comparable-run median was {baseline:g}",
                        ))
                    elif baseline >= 4 and len(comparable) >= 2:
                        threshold = max(1.0, baseline * 0.25)
                        latest_two = [row["items_found"] for row in comparable[-2:]]
                        if len(latest_two) == 2 and all(value <= threshold for value in latest_two):
                            incidents.append(_incident(
                                slug, "yield_drop", severity,
                                f"last two comparable runs found {latest_two}; prior median was {baseline:g}",
                            ))

            metrics.append({
                "kind": kind,
                "source": slug,
                "organization": entry.get("organization"),
                "latest_run_id": latest_run["id"] if latest_run else None,
                "latest_status": current["status"] if current else "missing",
                "latest_found": current["items_found"] if current else None,
                "latest_stored": current["items_stored"] if current else None,
                "recent_statuses": statuses,
                "zero_streak": zero_streak,
                "failure_streak": failure_streak,
                "comparable_baseline_runs": len(previous),
                "baseline_median_found": baseline,
                "baseline_state": "ready" if baseline_ready else "learning",
            })
    return metrics, incidents, learning


def _body_metrics(conn: sqlite3.Connection, latest_runs: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not _table_exists(conn, "body_fetch"):
        return {"available": False}, []
    totals = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM body_fetch GROUP BY status"
        )
    }
    by_source = [
        dict(row)
        for row in conn.execute(
            "SELECT source_slug, status, COUNT(*) AS n FROM body_fetch "
            "GROUP BY source_slug, status ORDER BY source_slug, status"
        )
    ]
    stuck = [
        dict(row)
        for row in conn.execute(
            "SELECT source_slug, COUNT(*) AS n, MAX(attempts) AS max_attempts "
            "FROM body_fetch WHERE status = 'failed' AND attempts >= 3 "
            "GROUP BY source_slug ORDER BY source_slug"
        )
    ]
    incidents = [
        _incident(
            row["source_slug"],
            "repeated_body_failures",
            "warning",
            f"{row['n']} body URL(s) remain retryable after at least three attempts "
            f"(maximum {row['max_attempts']})",
        )
        for row in stuck
    ]
    latest_body_runs = {
        kind: run["id"]
        for kind, run in latest_runs.items()
        if kind.startswith("bodies:")
    }
    outcomes = []
    for kind, run_id in sorted(latest_body_runs.items()):
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM body_fetch WHERE last_run_id = ? "
                "GROUP BY status",
                (run_id,),
            )
        }
        outcomes.append({"kind": kind, "run_id": run_id, "current_outcomes": counts})
    return {
        "available": True,
        "queue_totals": totals,
        "by_source": by_source,
        "latest_runs": outcomes,
    }, incidents


def _load_canary(path: Path, *, cycle_date: str, expected_news_run: int | None) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [_incident(
            "publisher-canaries", "canary_snapshot_missing", "critical",
            f"canary snapshot does not exist at {path}",
        )]
    except (OSError, json.JSONDecodeError) as exc:
        return None, [_incident(
            "publisher-canaries", "canary_snapshot_invalid", "critical",
            f"cannot read canary snapshot: {exc}",
        )]
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return None, [_incident(
            "publisher-canaries", "canary_snapshot_invalid", "critical",
            "canary snapshot is not a schema_version 1 object",
        )]
    if raw.get("cycle_date") != cycle_date or raw.get("news_run_id") != expected_news_run:
        return raw, [_incident(
            "publisher-canaries", "canary_snapshot_mismatch", "critical",
            f"canary observed cycle {raw.get('cycle_date')} / news run "
            f"{raw.get('news_run_id')}, expected {cycle_date} / {expected_news_run}",
        )]
    incidents = raw.get("incidents")
    if not isinstance(incidents, list):
        return raw, [_incident(
            "publisher-canaries", "canary_snapshot_invalid", "critical",
            "canary incidents is not a list",
        )]
    accepted = []
    for incident in incidents:
        if not isinstance(incident, dict) or incident.get("severity") not in {"warning", "critical"}:
            return raw, [_incident(
                "publisher-canaries", "canary_snapshot_invalid", "critical",
                "canary contains an invalid incident",
            )]
        accepted.append({
            "source": str(incident.get("source") or "publisher-canaries"),
            "check": str(incident.get("check") or "canary"),
            "severity": incident["severity"],
            "message": str(incident.get("message") or "canary reported an incident"),
        })
    return raw, accepted


def _config_hash(paths_and_bytes: list[tuple[Path, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, raw in sorted(paths_and_bytes, key=lambda item: str(item[0])):
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _incident_key(incidents: list[dict[str, str]]) -> str | None:
    if not incidents:
        return None
    identities = sorted(
        (item["severity"], item["source"], item["check"]) for item in incidents
    )
    return hashlib.sha256(
        json.dumps(identities, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()[:16]


def analyze(
    *, db_path: Path, news_sources: Path, regulatory_sources: Path,
    canary_config: Path, canary_file: Path, output_dir: Path,
    cycle_date: str, generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or now_utc()
    news_raw = news_sources.read_bytes()
    regulatory_raw = regulatory_sources.read_bytes()
    expected = {
        "news": _source_entries(news_sources),
        "regulatory": _source_entries(regulatory_sources),
    }
    critical_sources, canary_config_raw = _load_canary_sources(canary_config)
    with _connect_readonly(db_path) as conn:
        latest_runs = _latest_runs(conn)
        rows = _run_source_rows(conn)
        watermarks = {
            row["scope"]: dict(row)
            for row in conn.execute("SELECT * FROM watermark")
        }
        source_metrics, incidents, learning = _source_metrics(
            expected=expected,
            latest_runs=latest_runs,
            rows=rows,
            watermarks=watermarks,
            critical_sources=critical_sources,
        )
        body_metrics, body_incidents = _body_metrics(conn, latest_runs)
    incidents.extend(body_incidents)
    news_run = latest_runs.get("news")
    canary, canary_incidents = _load_canary(
        canary_file,
        cycle_date=cycle_date,
        expected_news_run=news_run["id"] if news_run else None,
    )
    incidents.extend(canary_incidents)

    incidents.sort(key=lambda item: (-SEVERITY_RANK[item["severity"]], item["source"], item["check"]))
    if any(item["severity"] == "critical" for item in incidents):
        status = "critical"
    elif incidents:
        status = "warning"
    elif learning:
        status = "learning"
    else:
        status = "healthy"

    run_ids = {kind: run["id"] for kind, run in sorted(latest_runs.items())}
    cycle_id = f"{cycle_date}-news-{news_run['id'] if news_run else 'none'}"
    snapshot = {
        "schema_version": 1,
        "kind": "coverage_health",
        "rules_version": RULES_VERSION,
        "generated_utc": format_utc(generated),
        "cycle_date": cycle_date,
        "cycle_id": cycle_id,
        "run_ids": run_ids,
        "config_sha256": _config_hash([
            (news_sources, news_raw),
            (regulatory_sources, regulatory_raw),
            (canary_config, canary_config_raw),
        ]),
        "status": status,
        "incident_key": _incident_key(incidents),
        "incidents": incidents,
        "summary": {
            "configured_sources": sum(len(entries) for entries in expected.values()),
            "sources_learning_baseline": learning,
            "warnings": sum(item["severity"] == "warning" for item in incidents),
            "critical": sum(item["severity"] == "critical" for item in incidents),
        },
        "sources": source_metrics,
        "body_fetch": body_metrics,
        "canaries": canary,
    }
    history, latest = publish_snapshot(output_dir, cycle_id, snapshot)
    snapshot["_paths"] = {"history": str(history), "latest": str(latest)}
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--news-sources", type=Path, default=DEFAULT_NEWS_SOURCES)
    parser.add_argument("--regulatory-sources", type=Path, default=DEFAULT_REGULATORY_SOURCES)
    parser.add_argument("--canary-config", type=Path, default=DEFAULT_CANARY_CONFIG)
    parser.add_argument("--canary-file", type=Path, default=DEFAULT_CANARY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cycle-date", default=date.today().isoformat())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = analyze(
            db_path=args.db,
            news_sources=args.news_sources,
            regulatory_sources=args.regulatory_sources,
            canary_config=args.canary_config,
            canary_file=args.canary_file,
            output_dir=args.output_dir,
            cycle_date=args.cycle_date,
        )
    except Exception as exc:
        print(f"ERROR: coverage analyzer failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"[coverage-health] {result['status']}: "
        f"{result['summary']['warnings']} warning(s), "
        f"{result['summary']['critical']} critical incident(s), "
        f"{result['summary']['sources_learning_baseline']} source baseline(s) learning"
    )
    for incident in result["incidents"]:
        print(
            f"  {incident['severity']} {incident['source']} "
            f"{incident['check']}: {incident['message']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
