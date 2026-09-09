"""SQLite access and migration runner.

Migrations are ordered files in migrations/ named NNN_name.sql. Each is applied
once, inside a transaction, and recorded in schema_migration. There is no
down-migration: rolling back a schema change means writing a new file.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from src.config import DATA_DIR, ROOT
from src.logger import get_logger

logger = get_logger(__name__)

DB_PATH = DATA_DIR / "brandmonitor.sqlite3"
MIGRATIONS_DIR = ROOT / "migrations"


def utcnow() -> str:
    """Timestamps are stored as UTC ISO-8601 so they sort lexicographically."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The pipeline writes from several threads and reads while writing.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}


def migrate(path: Optional[Path] = None, migrations_dir: Optional[Path] = None) -> list[str]:
    """Apply any unapplied migrations in filename order. Returns what was applied."""
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    files = sorted(directory.glob("*.sql"))
    applied_now: list[str] = []

    with session(path) as conn:
        done = _applied(conn)
        for f in files:
            if f.name in done:
                continue
            logger.info("[db] applying migration %s", f.name)
            conn.executescript(f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migration (name, applied_at) VALUES (?, ?)",
                (f.name, utcnow()),
            )
            applied_now.append(f.name)
    return applied_now


# ── watermarks ────────────────────────────────────────────────────────────

def get_watermark(conn: sqlite3.Connection, scope: str) -> Optional[str]:
    row = conn.execute(
        "SELECT position FROM watermark WHERE scope = ?", (scope,)
    ).fetchone()
    return row["position"] if row else None


def set_watermark(conn: sqlite3.Connection, scope: str, position: str) -> None:
    conn.execute(
        "INSERT INTO watermark (scope, position, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(scope) DO UPDATE SET position = excluded.position, "
        "updated_at = excluded.updated_at",
        (scope, position, utcnow()),
    )


# ── runs ──────────────────────────────────────────────────────────────────

def start_run(conn: sqlite3.Connection, kind: str,
              window_start: str, window_end: str) -> int:
    cur = conn.execute(
        "INSERT INTO run (kind, started_at, status, window_start, window_end) "
        "VALUES (?, ?, 'running', ?, ?)",
        (kind, utcnow(), window_start, window_end),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str,
               note: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE run SET finished_at = ?, status = ?, note = ? WHERE id = ?",
        (utcnow(), status, note, run_id),
    )


def record_source_result(conn: sqlite3.Connection, run_id: int, source_slug: str,
                         status: str, items_found: int = 0, items_stored: int = 0,
                         error: Optional[str] = None) -> None:
    """Record one source's outcome. 'zero' means looked and found nothing."""
    conn.execute(
        "INSERT INTO run_source (run_id, source_slug, status, items_found, "
        "items_stored, error) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, source_slug) DO UPDATE SET status = excluded.status, "
        "items_found = excluded.items_found, items_stored = excluded.items_stored, "
        "error = excluded.error",
        (run_id, source_slug, status, items_found, items_stored, error),
    )
