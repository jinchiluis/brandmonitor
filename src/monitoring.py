"""Operational monitoring: record what the pipeline did, never change what it does.

The 06:00 run on 2026-09-16 stored its news collection as ``ok``: 23 of 24 sources
reported ``zero`` with no error, each logging ``0 request(s), 0.00 MB``, because
the requests that failed never produced a response to count and the vendored
crawler swallowed the exceptions. Nothing recorded which files were asked for,
what answered, or what each file held, so a network failure and a quiet night
looked identical. This module records that, into the tables of
``migrations/007_monitoring.sql``:

``pipeline_pass``     one batch invocation, including offline and lock-skipped slots
``discovery_source``  one source's discovery within a collection run: cost, window,
                      watermark before and after, drop counts
``fetch_event``       every request a discovery pass started, and what a sitemap or
                      feed file yielded
``body_attempt``      every body fetch attempt and what the queue made of it

The contract with the pipeline is one-way. Recording happens after the production
commit it describes, in its own session, and every public entry point here
swallows its own failures: a monitoring bug or a locked table costs a monitoring
row and a log line, never a stored item, a watermark or an exit code. Nothing in
the pipeline reads these tables.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from requests.models import PreparedRequest

from src.config import MONITORING_RETENTION_DAYS, ROOT
from src.db import get_watermark, session, utcnow
from src.logger import get_logger

logger = get_logger(__name__)

ERROR_CHARS = 500
PASS_KINDS = ("daily", "intraday")
PASS_OUTCOMES = ("completed", "offline", "lock_skipped")


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]


def _prepared_url(url: str) -> str:
    """The URL as requests sends it, which is what the adapter sees."""
    try:
        prepared = PreparedRequest()
        prepared.prepare_url(url, None)
        return prepared.url
    except Exception:  # noqa: BLE001 - a lookup key, not a request
        return url


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class FetchRecorder:
    """Every request one source's discovery pass starts, and what each file held.

    Given to ``PoliteAdapter`` (transport: every attempt, answered or not) and to
    ``DiscoveryCache`` (content: the entries a parsed or replayed file yielded).
    A file's entries are attached to the latest request for its URL, following
    redirects to the hop that answered. Every method is safe to call with anything
    and never raises into the crawl.
    """

    def __init__(self, since: Optional[datetime] = None):
        self.since = since
        self.started_at = utcnow()
        self.finished_at: Optional[str] = None
        self.events: List[Dict[str, Any]] = []
        self._by_url: Dict[str, Dict[str, Any]] = {}
        self._redirects: Dict[str, str] = {}
        self._lock = threading.Lock()

    def begin(self, request: Any) -> Optional[Dict[str, Any]]:
        try:
            url = request.url
            headers = request.headers or {}
            with self._lock:
                origin = self._redirects.pop(url, None)
                event = {
                    "seq": len(self.events) + 1, "at": utcnow(),
                    "method": request.method or "GET", "url": url,
                    "redirected_from": origin,
                    "conditional": int("If-None-Match" in headers
                                       or "If-Modified-Since" in headers),
                    "status": None, "elapsed_ms": None, "bytes": None,
                    "content_type": None, "error": None, "parse": None,
                    "entries": None, "children": None, "dated_entries": None,
                    "entries_since": None, "newest_entry": None, "oldest_entry": None,
                    "_t0": time.monotonic(),
                }
                self.events.append(event)
                self._by_url[url] = event
                if origin:
                    self._by_url[origin] = event
            return event
        except Exception:  # noqa: BLE001 - monitoring never raises into a crawl
            return None

    def finish(self, event: Optional[Dict[str, Any]], *, response: Any = None,
               error: Optional[BaseException] = None) -> None:
        if event is None:
            return
        try:
            event["elapsed_ms"] = int((time.monotonic() - event.pop("_t0")) * 1000)
            self.finished_at = utcnow()
            if error is not None:
                event["error"] = _error_text(error)
                return
            event["status"] = response.status_code
            event["bytes"] = len(response.content or b"")
            event["content_type"] = (response.headers.get("Content-Type") or None)
            if response.is_redirect:
                target = _prepared_url(urljoin(event["url"], response.headers["Location"]))
                with self._lock:
                    self._redirects[target] = event["redirected_from"] or event["url"]
        except Exception:  # noqa: BLE001
            return

    def file_read(self, url: str, how: str, entries: Iterable[Any],
                  children: Iterable[Any]) -> None:
        """Attach what a sitemap or feed file yielded to the request that fetched it.

        ``how`` is ``parsed`` or ``replayed``. The first report for a request wins:
        the pinned path replays a 304 and then remembers the same entries again.
        A request that failed or answered 4xx/5xx is not a parse, even when a
        traversal hands the cache its empty result.
        """
        try:
            with self._lock:
                event = self._by_url.get(url) or self._by_url.get(_prepared_url(url))
            if event is None or event["parse"] is not None:
                return
            if event["error"] or (event["status"] or 0) >= 400:
                return
            entries = list(entries)
            dates = [entry[1] for entry in entries if entry[1] is not None]
            event["parse"] = how
            event["entries"] = len(entries)
            event["children"] = len(list(children))
            event["dated_entries"] = len(dates)
            if dates:
                event["newest_entry"] = _iso(max(dates))
                event["oldest_entry"] = _iso(min(dates))
                if self.since is not None:
                    event["entries_since"] = sum(1 for when in dates if when >= self.since)
        except Exception:  # noqa: BLE001 - e.g. naive and aware dates in one file
            return

    def counts(self) -> Dict[str, int]:
        return {"attempts": len(self.events),
                "transport_errors": sum(1 for e in self.events if e["error"])}


# ── writers ───────────────────────────────────────────────────────────────

def record_discovery(db_path: Optional[Path], run_id: int, slug: str, *,
                     status: str, window_start: Optional[str], window_end: str,
                     watermark_scope: str, watermark_before: Optional[str],
                     hints: List[Any], stored: int, error: Optional[str],
                     http: Optional[Dict[str, Any]]) -> None:
    """Write one source's ``discovery_source`` row and its ``fetch_event`` rows."""
    try:
        # Imported here: src.collect imports this module.
        from src.collect import is_furniture, is_malformed

        http = http or {}
        recorder: Optional[FetchRecorder] = http.get("recorder")
        counts = recorder.counts() if recorder else {}
        index_pages = sum(1 for hint in hints if is_furniture(hint.url))
        malformed = sum(1 for hint in hints
                        if is_malformed(hint.url) and not is_furniture(hint.url))
        with session(db_path) as conn:
            after = get_watermark(conn, watermark_scope)
            conn.execute(
                "INSERT OR REPLACE INTO discovery_source (run_id, source_slug, status, "
                "started_at, finished_at, window_start, window_end, since, "
                "watermark_before, watermark_after, watermark_advanced, attempts, "
                "responses, transport_errors, not_modified, replayed, bytes, throttles, "
                "hints, excluded, index_pages, malformed, stored, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, slug, status,
                 recorder.started_at if recorder else None,
                 (recorder.finished_at or recorder.started_at) if recorder else None,
                 window_start, window_end,
                 _iso(recorder.since) if recorder else None,
                 watermark_before, after, int(after != watermark_before),
                 counts.get("attempts"), http.get("requests"),
                 counts.get("transport_errors"), http.get("not_modified"),
                 http.get("replayed"), http.get("bytes"), http.get("throttles"),
                 len(hints), http.get("excluded"), index_pages, malformed, stored, error),
            )
            if recorder and recorder.events:
                conn.executemany(
                    "INSERT INTO fetch_event (run_id, source_slug, seq, at, method, url, "
                    "redirected_from, conditional, status, elapsed_ms, bytes, "
                    "content_type, error, parse, entries, children, dated_entries, "
                    "entries_since, newest_entry, oldest_entry) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(run_id, slug, e["seq"], e["at"], e["method"], e["url"],
                      e["redirected_from"], e["conditional"], e["status"],
                      e["elapsed_ms"], e["bytes"], e["content_type"], e["error"],
                      e["parse"], e["entries"], e["children"], e["dated_entries"],
                      e["entries_since"], e["newest_entry"], e["oldest_entry"])
                     for e in recorder.events],
                )
    except Exception as exc:  # noqa: BLE001 - never costs the collection anything
        logger.warning("[monitoring] could not record discovery for %s in run %s: %s",
                       slug, run_id, _error_text(exc))


def record_body_attempt(db_path: Optional[Path], run_id: int, task: Any, result: Any, *,
                        at: str, elapsed_ms: int, counted: bool,
                        attempts: Optional[int], stored: int) -> None:
    """Write one ``body_attempt`` row for a body fetch the queue has already committed."""
    try:
        with session(db_path) as conn:
            conn.execute(
                "INSERT INTO body_attempt (run_id, source_slug, external_id, at, "
                "elapsed_ms, status, error, transport, counted, attempts, final_url, "
                "stored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task["source_slug"], task["external_id"], at, elapsed_ms,
                 result.status, (result.error or None) and result.error[:ERROR_CHARS],
                 int(bool(result.transport)), int(counted), attempts, result.url,
                 stored),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[monitoring] could not record body attempt for %s: %s",
                       task["external_id"] if task is not None else "?", _error_text(exc))


# ── passes ────────────────────────────────────────────────────────────────

CONFIG_GLOBS = ("config.json", "input/*.json")


def config_fingerprint(root: Path = ROOT) -> tuple[Optional[str], Dict[str, str]]:
    """sha256 of every collection config file, and of them together."""
    files: Dict[str, str] = {}
    for pattern in CONFIG_GLOBS:
        for path in sorted(root.glob(pattern)):
            try:
                files[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
            except OSError:
                continue
    if not files:
        return None, files
    combined = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return combined, files


def git_commit(root: Path = ROOT) -> Optional[str]:
    """The checked-out commit, read from .git without running git."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip() or None
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        return None
    return None


def git_dirty(root: Path = ROOT, timeout: float = 20.0) -> Optional[List[str]]:
    """Modified tracked files, or None when git cannot say.

    ``--no-optional-locks`` keeps ``git status`` from refreshing the index, so
    recording a pass writes nothing into the repository.
    """
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain",
             "--untracked-files=no"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]


def _utc(value: Optional[str]) -> Optional[str]:
    """Normalise a batch file's UTC stamp (``...T04:00:01Z`` or naive) to utcnow's form."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def prune_monitoring(db_path: Optional[Path] = None,
                     retention_days: int = MONITORING_RETENTION_DAYS,
                     now: Optional[datetime] = None) -> Dict[str, int]:
    """Delete monitoring rows older than the rolling window. Returns rows removed per table.

    Only the four monitoring tables. A run's discovery, request and body rows go
    together, judged by the run's start, so a run is never left half described.
    Comparisons go through julianday() because stored stamps carry offsets.
    """
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
              ).astimezone(timezone.utc).isoformat(timespec="seconds")
    old_runs = "SELECT id FROM run WHERE julianday(started_at) < julianday(?)"
    removed: Dict[str, int] = {}
    with session(db_path) as conn:
        for table in ("fetch_event", "discovery_source", "body_attempt"):
            removed[table] = conn.execute(
                f"DELETE FROM {table} WHERE run_id IN ({old_runs})", (cutoff,)).rowcount
        removed["pipeline_pass"] = conn.execute(
            "DELETE FROM pipeline_pass WHERE julianday(finished_at) < julianday(?)",
            (cutoff,)).rowcount
    return removed


def record_pass(*, kind: str, outcome: str, started: Optional[str] = None,
                marker: Optional[Path] = None, db_path: Optional[Path] = None,
                root: Path = ROOT, prune: Optional[bool] = None) -> Optional[int]:
    """Write one ``pipeline_pass`` row. Returns its id, or None if it could not.

    ``marker`` is the ``last_run.json`` / ``last_intraday_run.json`` the batch file
    has just written for this pass; its stage codes, worst exit, cycle date and log
    path are copied. A lock-skipped slot writes no marker, so it has none.

    A daily pass also prunes the monitoring tables to their rolling window
    (``prune_monitoring``), once the pass row itself is safely written. ``prune``
    overrides that choice.
    """
    pass_id = _insert_pass(kind=kind, outcome=outcome, started=started, marker=marker,
                           db_path=db_path, root=root)
    if pass_id is not None and (kind == "daily" if prune is None else prune):
        try:
            removed = prune_monitoring(db_path)
            if any(removed.values()):
                logger.info("[monitoring] pruned rows older than %d days: %s",
                            MONITORING_RETENTION_DAYS, removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[monitoring] prune failed: %s", _error_text(exc))
    return pass_id


def _insert_pass(*, kind: str, outcome: str, started: Optional[str],
                 marker: Optional[Path], db_path: Optional[Path],
                 root: Path) -> Optional[int]:
    try:
        if kind not in PASS_KINDS or outcome not in PASS_OUTCOMES:
            raise ValueError(f"unknown pass kind/outcome: {kind}/{outcome}")
        data: Dict[str, Any] = {}
        if marker is not None:
            try:
                data = json.loads(Path(marker).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                logger.warning("[monitoring] pass marker %s unreadable: %s", marker,
                               _error_text(exc))
        config_hash, config_files = config_fingerprint(root)
        dirty = git_dirty(root)
        with session(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_pass (kind, outcome, started_at, finished_at, "
                "cycle_date, worst_exit, stages, log_path, git_commit, git_dirty, "
                "config_hash, config_files, host, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, outcome, _utc(started),
                 _utc(data.get("finished_utc")) or utcnow(),
                 data.get("cycle_date"), data.get("worst_exit"),
                 json.dumps(data.get("stages") or {}), data.get("log"),
                 git_commit(root), None if dirty is None else json.dumps(dirty),
                 config_hash, json.dumps(config_files), socket.gethostname(), utcnow()),
            )
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[monitoring] could not record %s pass (%s): %s", kind, outcome,
                       _error_text(exc))
        return None
