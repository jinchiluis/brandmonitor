#!/usr/bin/env python3
"""Read-only monitoring server: passes, sources, sitemap files, bodies, gates and logs.

Collection fails in ways the exit codes cannot show - on 2026-09-16 at 06:00, 23 of
24 news sources were stored as ``zero`` because nothing answered. This serves one
page (``tools/admin_ui.html``) and a small JSON API over what the pipeline records:
``run``/``run_source``, the monitoring tables of ``src/monitoring.py``,
``body_fetch``, the gates' decisions, the title-gate JSONL, the run markers, the
health verdict and the log files.

It changes nothing. The database is opened ``mode=ro`` with ``query_only``, only
GET is served, files are read from ``data/`` alone, and the scheduled-task query
is ``Get-ScheduledTask``. It imports nothing from ``src/``, so it runs from any
checkout against any data directory.

    python tools/admin.py                        # on the laptop: http://127.0.0.1:8765
    python tools/laptop.py admin                 # elsewhere: runs it there, tunnels here
    python tools/admin.py --data D:/copy/data    # a restored snapshot

It binds to 127.0.0.1 by default, and there is no login: reach it through the SSH
tunnel, not by binding a public address.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
UI_PATH = Path(__file__).resolve().parent / "admin_ui.html"
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
MAX_DAYS = 30
LOG_BYTES = 3_000_000
TASKS = ("brandmonitor-daily", "brandmonitor-intraday")
STRUCTURED_KINDS = ("dip", "ep_procedures", "safety_gate", "dsa", "dip_documents")
DAILY_ONLY_KINDS = {"regulatory", "dip", "ep_procedures", "safety_gate", "dip_documents"}
# A gap this long between two stage runs starts a new derived pass.
PASS_GAP = timedelta(minutes=30)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def berlin_day(value: Optional[str]) -> Optional[str]:
    parsed = parse_time(value)
    return parsed.astimezone(BERLIN).date().isoformat() if parsed else None


def slug_for(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


class Store:
    """Everything the server reads, rooted at one data directory."""

    def __init__(self, data: Path, inputs: Path, repo: Path):
        self.data = data
        self.inputs = inputs
        self.repo = repo
        self.db_path = data / "brandmonitor.sqlite3"
        self._tasks: Tuple[float, Any] = (0.0, None)
        self._jsonl: Dict[Path, Tuple[float, int, List[dict]]] = {}
        self._lock = threading.Lock()

    # ── database ──────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"no database at {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path.resolve().as_posix()}?mode=ro", uri=True,
                               timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    @staticmethod
    def tables(conn: sqlite3.Connection) -> set:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def rows(conn: sqlite3.Connection, sql: str, *params: Any) -> List[dict]:
        return [dict(row) for row in conn.execute(sql, params)]

    # ── files ─────────────────────────────────────────────────────────────

    def read_json(self, relative: str) -> Optional[Any]:
        path = self.data / relative
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def sources(self) -> Dict[str, Dict[str, dict]]:
        """Configured sources by kind and slug, as the collector derives slugs."""
        out: Dict[str, Dict[str, dict]] = {}
        for kind, name in (("news", "germany_medias.json"),
                           ("regulatory", "regulatory_sources.json")):
            try:
                entries = json.loads((self.inputs / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                entries = []
            out[kind] = {slug_for(e["url"]): e for e in entries
                         if isinstance(e, dict) and e.get("url")}
        return out

    def title_gate_decisions(self, since_day: date) -> List[dict]:
        """Title-gate JSONL decisions from files dated on or after ``since_day``."""
        root = self.data / "title_gate"
        out: List[dict] = []
        if not root.exists():
            return out
        for path in sorted(root.glob("*/*.jsonl")):
            try:
                if date.fromisoformat(path.stem) < since_day:
                    continue
                stat = path.stat()
            except (ValueError, OSError):
                continue
            with self._lock:
                cached = self._jsonl.get(path)
            if cached and cached[:2] == (stat.st_mtime, stat.st_size):
                out.extend(cached[2])
                continue
            decisions = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            decisions.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                continue
            with self._lock:
                self._jsonl[path] = (stat.st_mtime, stat.st_size, decisions)
            out.extend(decisions)
        return out

    def git_head(self) -> Optional[str]:
        git = self.repo / ".git"
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref: "):
                return head
            ref = git / head[5:]
            if ref.exists():
                return ref.read_text(encoding="utf-8").strip()
            for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + head[5:]):
                    return line.split()[0]
        except OSError:
            return None
        return None

    def scheduled_tasks(self) -> Optional[List[dict]]:
        """Task Scheduler state for the two tasks, cached for a minute. Windows only."""
        if os.name != "nt":
            return None
        with self._lock:
            stamp, value = self._tasks
        if time.monotonic() - stamp < 60:
            return value
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$out=@();foreach($n in @('" + "','".join(TASKS) + "')){"
            "$t=Get-ScheduledTask -TaskName $n;"
            "if($t){$i=$t|Get-ScheduledTaskInfo;"
            "$out+=[pscustomobject]@{name=$n;state=[string]$t.State;"
            "enabled=[bool]$t.Settings.Enabled;"
            "last_run=$(if($i.LastRunTime){$i.LastRunTime.ToUniversalTime().ToString('o')});"
            "next_run=$(if($i.NextRunTime){$i.NextRunTime.ToUniversalTime().ToString('o')});"
            "last_result=$i.LastTaskResult;missed=$i.NumberOfMissedRuns}}"
            "else{$out+=[pscustomobject]@{name=$n;state='not registered'}}};"
            "ConvertTo-Json -InputObject @($out) -Compress")
        try:
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                   script], capture_output=True, text=True, timeout=30)
            value = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            value = None
        with self._lock:
            self._tasks = (time.monotonic(), value)
        return value


# ── helpers over runs ─────────────────────────────────────────────────────

def window(params: Dict[str, str]) -> Tuple[int, datetime]:
    try:
        days = int(params.get("days", "7"))
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "days must be a number")
    days = max(1, min(MAX_DAYS, days))
    return days, datetime.now(UTC) - timedelta(days=days)


def runs_since(conn: sqlite3.Connection, cutoff: datetime,
               kinds: Optional[Iterable[str]] = None) -> List[dict]:
    sql = ("SELECT r.id, r.kind, r.started_at, r.finished_at, r.status, r.window_start, "
           "r.window_end, r.note, COUNT(s.id) AS sources, "
           "SUM(s.status='ok') AS ok, SUM(s.status='zero') AS zero, "
           "SUM(s.status='failed') AS failed, SUM(s.status='paused') AS paused, "
           "SUM(s.items_found) AS found, SUM(s.items_stored) AS stored "
           "FROM run r LEFT JOIN run_source s ON s.run_id = r.id "
           "WHERE julianday(r.started_at) >= julianday(?)")
    params: List[Any] = [iso(cutoff)]
    if kinds is not None:
        kinds = list(kinds)
        sql += f" AND r.kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    sql += " GROUP BY r.id ORDER BY r.started_at"
    return Store.rows(conn, sql, *params)


def group_passes(recorded: List[dict], runs: List[dict]) -> List[dict]:
    """Attach stage runs to recorded passes, and derive passes for the rest.

    A recorded pass owns every run that started inside its span; the run lock makes
    scheduled passes disjoint. Runs outside any recorded pass - everything before
    monitoring was deployed, and manual commands - are grouped into derived passes:
    a news collection or a 30-minute gap starts a new one.
    """
    passes = []
    for row in recorded:
        start = parse_time(row["started_at"]) or parse_time(row["finished_at"])
        end = parse_time(row["finished_at"])
        passes.append(dict(row, derived=False, runs=[], _span=(start, end),
                           stages=json.loads(row.get("stages") or "{}"),
                           git_dirty=json.loads(row["git_dirty"]) if row.get("git_dirty") else None,
                           config_files=None))
    loose = []
    for run in runs:
        started = parse_time(run["started_at"])
        owner = next((p for p in passes if p["_span"][0] and p["_span"][1]
                      and p["_span"][0] - timedelta(seconds=5) <= started
                      <= p["_span"][1] + timedelta(seconds=5)), None)
        if owner:
            owner["runs"].append(run)
        else:
            loose.append(run)

    group: List[dict] = []
    last_end: Optional[datetime] = None

    def flush():
        if not group:
            return
        kinds = {r["kind"] for r in group}
        begin = parse_time(group[0]["started_at"])
        finish = max((parse_time(r["finished_at"]) or parse_time(r["started_at"])) for r in group)
        led_by_news = group[0]["kind"] == "news"
        kind = ("daily" if kinds & DAILY_ONLY_KINDS and led_by_news else
                "intraday" if led_by_news else "manual")
        failed = any(r["status"] == "failed" for r in group)
        running = any(r["status"] == "running" for r in group)
        passes.append({
            "id": f"d{group[0]['id']}", "kind": kind, "outcome": "running" if running else "derived",
            "started_at": iso(begin), "finished_at": iso(finish), "worst_exit": None,
            "stages": {}, "derived": True, "runs": list(group), "run_failed": failed,
            "_span": (begin, finish),
        })
        group.clear()

    for run in loose:
        started = parse_time(run["started_at"])
        if group and (run["kind"] == "news" or (last_end and started - last_end > PASS_GAP)):
            flush()
        group.append(run)
        end = parse_time(run["finished_at"]) or started
        last_end = max(last_end, end) if last_end and group[:-1] else end
    flush()

    for p in passes:
        p.pop("_span", None)
        p["run_ids"] = [r["id"] for r in p["runs"]]
        p["failed_runs"] = [r["id"] for r in p["runs"] if r["status"] == "failed"]
    passes.sort(key=lambda p: p["started_at"] or p["finished_at"] or "")
    return passes


def classify(rs: dict) -> str:
    """One word for a source's outcome in one run, from run_source + discovery_source."""
    status = rs.get("status")
    if status == "paused":
        return "paused"
    if status == "failed":
        return "failed"
    attempts, responses = rs.get("attempts"), rs.get("responses")
    answered_nothing = attempts is not None and attempts > 0 and not responses
    if status == "zero":
        return "blind" if answered_nothing else "zero"
    if (rs.get("error") or "").startswith("truncated:"):
        return "truncated"
    if answered_nothing or (rs.get("transport_errors") or 0) or (rs.get("bad_requests") or 0):
        return "partial"
    return "new" if (rs.get("items_stored") or 0) > 0 else "seen"


# ── API ───────────────────────────────────────────────────────────────────

def api_overview(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    with store.connect() as conn:
        tables = store.tables(conn)
        monitoring = "pipeline_pass" in tables
        recorded = (store.rows(conn, "SELECT * FROM pipeline_pass WHERE "
                               "julianday(COALESCE(started_at, finished_at)) >= julianday(?) "
                               "ORDER BY finished_at", iso(cutoff)) if monitoring else [])
        runs = runs_since(conn, cutoff)
        queue = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM body_fetch GROUP BY status")} if "body_fetch" in tables else {}
        unsent = conn.execute(
            "SELECT COUNT(*) FROM alert_decision WHERE potential_alert=1 AND sent_at IS NULL"
        ).fetchone()[0] if "alert_decision" in tables else None
        migrations = [r[0] for r in conn.execute("SELECT name FROM schema_migration ORDER BY name")] \
            if "schema_migration" in tables else []
    passes = group_passes(recorded, runs)
    health = store.read_json("health/latest.json")
    try:
        size = store.db_path.stat().st_size
    except OSError:
        size = None
    return {
        "generated_at": iso(datetime.now(UTC)), "days": days,
        "db": {"path": str(store.db_path), "bytes": size, "monitoring": monitoring,
               "migrations": migrations},
        "checkout": {"commit": store.git_head(), "repo": str(store.repo)},
        "markers": {"daily": store.read_json("last_run.json"),
                    "intraday": store.read_json("last_intraday_run.json")},
        "health": None if health is None else {
            "status": health.get("status"), "generated_utc": health.get("generated_utc"),
            "summary": health.get("summary"), "incidents": health.get("incidents") or []},
        "tasks": store.scheduled_tasks(),
        "passes": passes,
        "running": [r for r in runs if r["status"] == "running"],
        "body_queue": queue, "unsent_alerts": unsent,
    }


def api_grid(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    kind = params.get("kind", "news")
    if kind not in ("news", "regulatory"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "kind must be news or regulatory")
    configured = store.sources().get(kind, {})
    with store.connect() as conn:
        tables = store.tables(conn)
        runs = runs_since(conn, cutoff, [kind])
        ids = [r["id"] for r in runs]
        cells: Dict[str, Dict[int, dict]] = defaultdict(dict)
        if ids:
            marks = ",".join("?" * len(ids))
            detail = ("LEFT JOIN discovery_source d ON d.run_id = s.run_id AND "
                      "d.source_slug = s.source_slug " if "discovery_source" in tables else "")
            columns = ("d.attempts, d.responses, d.transport_errors, d.not_modified, d.replayed, "
                       "d.bytes, d.throttles, d.excluded, d.index_pages, d.malformed, "
                       "d.watermark_advanced, d.started_at AS d_started, d.finished_at AS d_finished, "
                       if detail else "")
            for row in conn.execute(
                    f"SELECT s.run_id, s.source_slug, s.status, s.items_found, s.items_stored, "
                    f"{columns} s.error FROM run_source s {detail}WHERE s.run_id IN ({marks})", ids):
                cells[row["source_slug"]][row["run_id"]] = dict(row)
            if "fetch_event" in tables:
                for row in conn.execute(
                        f"SELECT run_id, source_slug, "
                        f"SUM(error IS NULL AND status >= 400 AND status NOT IN (404, 410)) AS bad, "
                        f"SUM(status IN (404, 410)) AS absent "
                        f"FROM fetch_event WHERE run_id IN ({marks}) GROUP BY run_id, source_slug", ids):
                    cell = cells.get(row["source_slug"], {}).get(row["run_id"])
                    if cell is not None:
                        cell["bad_requests"], cell["absent_files"] = row["bad"], row["absent"]
        watermarks = {row["scope"].rsplit(":", 1)[-1]: dict(row) for row in conn.execute(
            "SELECT scope, position, updated_at FROM watermark WHERE scope LIKE ?",
            (f"collection:{kind}:%",))}
    for by_run in cells.values():
        for cell in by_run.values():
            cell["class"] = classify(cell)
    slugs = sorted(set(cells) | {s for s, e in configured.items() if not e.get("collector")},
                   key=lambda s: ((configured.get(s) or {}).get("organization") or s).lower())
    sources = []
    for slug in slugs:
        entry = configured.get(slug) or {}
        sources.append({
            "slug": slug, "organization": entry.get("organization"),
            "configured": bool(entry), "collector": entry.get("collector"),
            "methods": [m for m in ("sitemap", "feeds", "frontpage", "brightdata") if entry.get(m)],
            "pins": len(entry.get("sitemap_urls") or []),
            "feed_urls": len(entry.get("feed_urls") or []),
            "content_mode": entry.get("content_mode", "title_only") if entry else None,
            "watermark": watermarks.get(slug),
        })
    return {"kind": kind, "days": days, "runs": runs, "sources": sources,
            "cells": {slug: {str(k): v for k, v in by_run.items()} for slug, by_run in cells.items()},
            "monitoring": "discovery_source" in tables}


def api_structured(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    with store.connect() as conn:
        runs = runs_since(conn, cutoff, STRUCTURED_KINDS)
        ids = [r["id"] for r in runs]
        detail = defaultdict(list)
        if ids:
            for row in conn.execute(
                    f"SELECT run_id, source_slug, status, items_found, items_stored, error "
                    f"FROM run_source WHERE run_id IN ({','.join('?' * len(ids))})", ids):
                detail[row["run_id"]].append(dict(row))
        latest_ok = {row["kind"]: row["finished_at"] for row in conn.execute(
            "SELECT kind, MAX(finished_at) finished_at FROM run WHERE status='ok' AND kind IN "
            f"({','.join('?' * len(STRUCTURED_KINDS))}) GROUP BY kind", STRUCTURED_KINDS)}
    for run in runs:
        run["sources_detail"] = detail.get(run["id"], [])
    return {"days": days, "runs": runs, "latest_ok": latest_ok}


def api_source(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    slug, kind = params.get("slug", ""), params.get("kind", "news")
    if not slug:
        raise ApiError(HTTPStatus.BAD_REQUEST, "slug is required")
    entry = store.sources().get(kind, {}).get(slug)
    since = iso(cutoff)
    with store.connect() as conn:
        tables = store.tables(conn)
        has_detail = "discovery_source" in tables
        detail_join = ("LEFT JOIN discovery_source d ON d.run_id=s.run_id AND d.source_slug=s.source_slug "
                       if has_detail else "")
        detail_cols = ("d.attempts, d.responses, d.transport_errors, d.not_modified, d.replayed, "
                       "d.bytes, d.throttles, d.hints, d.excluded, d.index_pages, d.malformed, "
                       "d.window_start, d.since, d.watermark_before, d.watermark_after, "
                       "d.watermark_advanced, d.started_at AS d_started, d.finished_at AS d_finished, "
                       if has_detail else "")
        runs = store.rows(conn,
                          f"SELECT r.id AS run_id, r.kind, r.started_at, r.finished_at, {detail_cols}"
                          f"s.status, s.items_found, s.items_stored, s.error FROM run_source s "
                          f"JOIN run r ON r.id=s.run_id {detail_join}"
                          f"WHERE s.source_slug=? AND r.kind=? AND julianday(r.started_at) >= julianday(?) "
                          f"ORDER BY r.started_at DESC", slug, kind, since)
        if "fetch_event" in tables and runs:
            bad = {row[0]: row[1] for row in conn.execute(
                "SELECT run_id, SUM(error IS NULL AND status >= 400 AND status NOT IN (404, 410)) "
                "FROM fetch_event WHERE source_slug=? GROUP BY run_id", (slug,))}
            for run in runs:
                run["bad_requests"] = bad.get(run["run_id"], 0)
        for run in runs:
            run["class"] = classify(run)
        files: Dict[str, dict] = {}
        if "fetch_event" in tables:
            for row in conn.execute(
                    "SELECT e.*, r.kind AS run_kind FROM fetch_event e JOIN run r ON r.id=e.run_id "
                    "WHERE e.source_slug=? AND julianday(e.at) >= julianday(?) "
                    "ORDER BY e.at DESC", (slug, since)):
                event = dict(row)
                key = event["redirected_from"] or event["url"]
                bucket = files.setdefault(key, {"url": key, "events": []})
                if len(bucket["events"]) < 60:
                    bucket["events"].append(event)
        scope = f"collection:{kind}:{slug}"
        watermark = conn.execute("SELECT position, updated_at FROM watermark WHERE scope=?",
                                 (scope,)).fetchone()
        per_day: Counter = Counter()
        versions: Counter = Counter()
        for row in conn.execute("SELECT fetched_at, version FROM raw_item WHERE source_slug=? "
                                "AND julianday(fetched_at) >= julianday(?)", (slug, since)):
            day = berlin_day(row["fetched_at"])
            (per_day if row["version"] == 1 else versions)[day] += 1
        items = store.rows(conn,
                           "SELECT id, version, title, url, published_at, fetched_at, first_run_id, "
                           "json_extract(payload, '$.published_at_source') AS date_source, "
                           "json_extract(payload, '$.discovered_via') AS discovered_via "
                           "FROM raw_item WHERE source_slug=? ORDER BY id DESC LIMIT 25", slug)
        bodies = {}
        if "body_fetch" in tables:
            bodies["queue"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM body_fetch WHERE source_slug=? GROUP BY status", (slug,))}
            bodies["problems"] = store.rows(
                conn, "SELECT external_id, status, attempts, attempted_at, error FROM body_fetch "
                      "WHERE source_slug=? AND status != 'ok' ORDER BY attempted_at DESC LIMIT 40", slug)
        if "body_attempt" in tables:
            bodies["attempts"] = store.rows(
                conn, "SELECT * FROM body_attempt WHERE source_slug=? AND julianday(at) >= julianday(?) "
                      "ORDER BY id DESC LIMIT 60", slug, since)
    decisions = [d for d in store.title_gate_decisions(cutoff.date()) if d.get("source") == slug]
    gate = Counter("fail_open" if d.get("fail_open") else d.get("decision") for d in decisions)
    day_list = [(datetime.now(BERLIN).date() - timedelta(days=n)).isoformat() for n in range(days)][::-1]
    return {
        "slug": slug, "kind": kind, "days": days, "entry": entry,
        "watermark": dict(watermark) if watermark else None,
        "runs": runs, "files": sorted(files.values(), key=lambda f: f["events"][0]["at"], reverse=True),
        "items_per_day": [{"day": d, "new": per_day.get(d, 0), "versions": versions.get(d, 0)}
                          for d in day_list],
        "latest_items": items, "bodies": bodies,
        "title_gate": {"counts": dict(gate),
                       "recent": sorted(decisions, key=lambda d: d.get("at") or "", reverse=True)[:40]},
        "monitoring": has_detail,
    }


def api_run(store: Store, params: Dict[str, str]) -> dict:
    try:
        run_id = int(params.get("id", ""))
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "id must be a run id")
    with store.connect() as conn:
        tables = store.tables(conn)
        run = conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no run {run_id}")
        run = dict(run)
        detail_join = ("LEFT JOIN discovery_source d ON d.run_id=s.run_id AND d.source_slug=s.source_slug "
                       if "discovery_source" in tables else "")
        detail_cols = ("d.attempts, d.responses, d.transport_errors, d.not_modified, d.replayed, "
                       "d.bytes, d.throttles, d.hints, d.excluded, d.index_pages, d.malformed, "
                       "d.window_start, d.since, d.watermark_before, d.watermark_after, "
                       "d.watermark_advanced, d.started_at AS d_started, d.finished_at AS d_finished, "
                       if detail_join else "")
        sources = store.rows(conn, f"SELECT {detail_cols}s.source_slug, s.status, s.items_found, "
                                   f"s.items_stored, s.error FROM run_source s {detail_join}"
                                   f"WHERE s.run_id=? ORDER BY s.source_slug", run_id)
        events = store.rows(conn, "SELECT * FROM fetch_event WHERE run_id=? ORDER BY source_slug, seq "
                                  "LIMIT 3000", run_id) if "fetch_event" in tables else []
        bad = Counter()
        for event in events:
            if event["error"] is None and (event["status"] or 0) >= 400 and event["status"] not in (404, 410):
                bad[event["source_slug"]] += 1
        for source in sources:
            source["bad_requests"] = bad.get(source["source_slug"], 0)
            source["class"] = classify(source) if run["kind"] in ("news", "regulatory") else None
        attempts = store.rows(conn, "SELECT * FROM body_attempt WHERE run_id=? ORDER BY id LIMIT 3000",
                              run_id) if "body_attempt" in tables else []
        stored = store.rows(conn, "SELECT id, source_slug, version, title, url, published_at, "
                                  "json_extract(payload, '$.published_at_source') AS date_source "
                                  "FROM raw_item WHERE first_run_id=? ORDER BY source_slug, id LIMIT 500",
                            run_id)
        stored_total = conn.execute("SELECT COUNT(*) FROM raw_item WHERE first_run_id=?",
                                    (run_id,)).fetchone()[0]
    started = parse_time(run["started_at"])
    decisions = ([d for d in store.title_gate_decisions((started - timedelta(days=1)).date())
                  if d.get("run_id") == run_id] if started and run["kind"] == "news" else [])
    return {"run": run, "sources": sources, "events": events, "body_attempts": attempts,
            "stored": stored, "stored_total": stored_total,
            "title_gate": {"counts": dict(Counter("fail_open" if d.get("fail_open") else d.get("decision")
                                                  for d in decisions)),
                           "decisions": decisions[:500]},
            "monitoring": "fetch_event" in tables}


def api_bodies(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    since = iso(cutoff)
    with store.connect() as conn:
        tables = store.tables(conn)
        if "body_fetch" not in tables:
            return {"days": days, "queue": [], "retired": [], "attempts_by_day": [], "runs": []}
        queue = store.rows(conn,
                           "SELECT b.source_slug, b.status, COUNT(*) AS n, MAX(b.attempts) AS max_attempts, "
                           "MIN(first.fetched_at) AS oldest_seen FROM body_fetch b "
                           "LEFT JOIN (SELECT source_slug, external_id, MIN(fetched_at) AS fetched_at "
                           "FROM raw_item GROUP BY source_slug, external_id) first "
                           "ON first.source_slug=b.source_slug AND first.external_id=b.external_id "
                           "GROUP BY b.source_slug, b.status ORDER BY b.source_slug")
        retired = store.rows(conn,
                             "SELECT source_slug, external_id, attempts, attempted_at, error FROM body_fetch "
                             "WHERE status='unavailable' AND julianday(attempted_at) >= julianday(?) "
                             "ORDER BY attempted_at DESC LIMIT 200", since)
        waiting = store.rows(conn,
                             "SELECT source_slug, external_id, status, attempts, attempted_at, error "
                             "FROM body_fetch WHERE status IN ('pending', 'failed') "
                             "ORDER BY attempted_at IS NOT NULL, attempted_at LIMIT 200")
        by_day: Dict[str, Counter] = defaultdict(Counter)
        recent: List[dict] = []
        if "body_attempt" in tables:
            for row in conn.execute("SELECT at, status, counted, transport FROM body_attempt "
                                    "WHERE julianday(at) >= julianday(?)", (since,)):
                counter = by_day[berlin_day(row["at"])]
                counter[row["status"]] += 1
                counter["transport"] += row["transport"]
                counter["uncounted"] += 0 if row["counted"] else 1
            recent = store.rows(conn, "SELECT * FROM body_attempt WHERE status != 'ok' AND "
                                      "julianday(at) >= julianday(?) ORDER BY id DESC LIMIT 150", since)
        runs = runs_since(conn, cutoff)
    return {"days": days, "queue": queue, "retired": retired, "waiting": waiting,
            "attempts_by_day": [{"day": day, **counts} for day, counts in sorted(by_day.items())],
            "recent_failures": recent,
            "runs": [r for r in runs if r["kind"].startswith("bodies")],
            "monitoring": "body_attempt" in tables}


def api_funnel(store: Store, params: Dict[str, str]) -> dict:
    days, cutoff = window(params)
    since = iso(cutoff)
    buckets: Dict[str, Counter] = defaultdict(Counter)
    with store.connect() as conn:
        tables = store.tables(conn)
        for row in conn.execute("SELECT fetched_at, source_kind, version FROM raw_item "
                                "WHERE julianday(fetched_at) >= julianday(?)", (since,)):
            kind = row["source_kind"] if row["source_kind"] in ("news", "regulatory") else "structured"
            buckets[berlin_day(row["fetched_at"])][f"{kind}_{'new' if row['version'] == 1 else 'versions'}"] += 1
        if "body_attempt" in tables:
            for row in conn.execute("SELECT at, status FROM body_attempt WHERE julianday(at) >= julianday(?)",
                                    (since,)):
                buckets[berlin_day(row["at"])][f"bodies_{row['status']}"] += 1
        if "assessment" in tables:
            for row in conn.execute(
                    "SELECT created_at, relevant, json_extract(payload, '$.fail_open') AS fail_open, "
                    "json_extract(payload, '$.kind') AS kind FROM assessment "
                    "WHERE json_extract(payload, '$.stage')='body_gate' AND julianday(created_at) >= julianday(?)",
                    (since,)):
                label = {1: "relevant", 0: "irrelevant"}.get(row["relevant"], "unsure")
                counter = buckets[berlin_day(row["created_at"])]
                counter[f"body_gate_{label}"] += 1
                counter["body_gate_fail_open"] += 1 if row["fail_open"] else 0
        unsent: List[dict] = []
        if "alert_decision" in tables:
            for row in conn.execute("SELECT created_at, potential_alert, sent_at FROM alert_decision "
                                    "WHERE julianday(created_at) >= julianday(?)", (since,)):
                counter = buckets[berlin_day(row["created_at"])]
                counter["alert_decided"] += 1
                counter["alert_potential"] += row["potential_alert"]
                counter["alert_sent"] += 1 if row["potential_alert"] and row["sent_at"] else 0
            unsent = store.rows(conn,
                                "SELECT a.id, a.source_slug, a.external_id, a.created_at, a.summary_zh, "
                                "r.title, r.url FROM alert_decision a JOIN raw_item r ON r.id=a.raw_item_id "
                                "WHERE a.potential_alert=1 AND a.sent_at IS NULL ORDER BY a.id DESC LIMIT 50")
        news_runs = runs_since(conn, cutoff, ["news"])
    per_run: Dict[Any, Counter] = defaultdict(Counter)
    for decision in store.title_gate_decisions(cutoff.date()):
        day = berlin_day(decision.get("at"))
        if day and decision.get("at") and parse_time(decision["at"]) >= cutoff:
            label = "fail_open" if decision.get("fail_open") else decision.get("decision")
            buckets[day][f"title_gate_{label}"] += 1
            per_run[decision.get("run_id")][label] += 1
    today = datetime.now(BERLIN).date()
    rows = [{"day": (today - timedelta(days=n)).isoformat(),
             **buckets.get((today - timedelta(days=n)).isoformat(), {})} for n in range(days)]
    return {"days": days, "by_day": rows,
            "news_runs": [dict(run, title_gate=dict(per_run.get(run["id"], {}))) for run in news_runs][::-1],
            "unsent_alerts": unsent}


def api_logs(store: Store, params: Dict[str, str]) -> dict:
    log_root = store.data / "log"
    dates = sorted((p.name for p in log_root.iterdir() if p.is_dir() and DATE_RE.match(p.name)),
                   reverse=True) if log_root.exists() else []
    chosen = params.get("date") or (dates[0] if dates else None)
    files = []
    if chosen:
        if not DATE_RE.match(chosen):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad date")
        folder = log_root / chosen
        if folder.exists():
            for path in sorted(folder.iterdir()):
                if path.is_file() and NAME_RE.match(path.name):
                    stat = path.stat()
                    files.append({"name": path.name, "bytes": stat.st_size,
                                  "modified": iso(datetime.fromtimestamp(stat.st_mtime, UTC))})
    return {"dates": dates, "date": chosen, "files": files}


def api_log(store: Store, params: Dict[str, str]) -> dict:
    day, name = params.get("date", ""), params.get("name", "")
    if not DATE_RE.match(day) or not NAME_RE.match(name):
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad date or name")
    folder = (store.data / "log" / day).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise ApiError(HTTPStatus.NOT_FOUND, "no such log")
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > LOG_BYTES:
            handle.seek(size - LOG_BYTES)
        raw = handle.read()
    return {"date": day, "name": name, "bytes": size, "truncated": size > LOG_BYTES,
            "text": raw.decode("utf-8", errors="replace")}


def api_health(store: Store, params: Dict[str, str]) -> dict:
    return {"latest": store.read_json("health/latest.json")}


ROUTES: Dict[str, Callable[[Store, Dict[str, str]], dict]] = {
    "/api/overview": api_overview, "/api/grid": api_grid, "/api/structured": api_structured,
    "/api/source": api_source, "/api/run": api_run, "/api/bodies": api_bodies,
    "/api/funnel": api_funnel, "/api/logs": api_logs, "/api/log": api_log,
    "/api/health": api_health,
}


# ── HTTP ──────────────────────────────────────────────────────────────────

def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "brandmonitor-admin"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                try:
                    self._send(HTTPStatus.OK, UI_PATH.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"missing {UI_PATH}"})
                return
            route = ROUTES.get(parsed.path)
            if route is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                self._json(HTTPStatus.OK, route(store, params))
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except sqlite3.Error as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"database: {exc}"})
            except Exception as exc:  # noqa: BLE001 - one bad view must not stop the server
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

        do_HEAD = do_GET

        def _refuse(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                sys.stderr.write(f"[admin] {self.address_string()} {format % args}\n")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default 127.0.0.1; there is no login)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / "data",
                        help="data directory holding brandmonitor.sqlite3, log/, health/")
    parser.add_argument("--inputs", type=Path, default=ROOT / "input",
                        help="source lists, for organisation names and methods")
    parser.add_argument("--repo", type=Path, default=ROOT,
                        help="checkout whose commit the overview shows")
    parser.add_argument("--exit-on-stdin-eof", action="store_true",
                        help="stop when stdin closes: an SSH session that started it has ended")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.data.resolve(), args.inputs.resolve(), args.repo.resolve())
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    server.daemon_threads = True
    print(f"[admin] serving http://{args.host}:{args.port}/  data={store.data}  (read-only)",
          flush=True)
    if args.exit_on_stdin_eof:
        def watch() -> None:
            try:
                while sys.stdin.read(1024):
                    pass
            except (OSError, ValueError):
                pass
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
