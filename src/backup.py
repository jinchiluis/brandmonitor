"""Online SQLite snapshot, compressed, rotated, and mirrored off the box.

Never copy the live database file. In WAL mode a committed transaction can still
be sitting in the ``-wal`` sidecar, so a plain copy of ``brandmonitor.sqlite3``
can be torn even when nothing looks busy. ``sqlite3.Connection.backup`` is
SQLite's own online backup API - the same mechanism behind the CLI's ``.backup``
- and copies a consistent snapshot page by page while the pipeline keeps
writing. That is why this is safe to run as a stage inside ``run_daily.bat``.

VACUUM before gzip is not cosmetic: measured on the 2026-09-10 database it took
the snapshot from 9.16 MB to 7.99 MB, a 13 per cent saving for about a second,
because ``.backup`` copies the freelist along with everything else.

Retention is tiered rather than a flat cutoff. The corpus is append-mostly, so
thirty consecutive daily snapshots are thirty near-identical copies of the same
bytes; spending the same space on daily/weekly/monthly tiers buys a year of
reach instead of a month.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

from src.config import (
    BACKUP_DIR,
    BACKUP_KEEP_DAILY,
    BACKUP_KEEP_MONTHLY,
    BACKUP_KEEP_WEEKLY,
    BACKUP_OFFBOX_DIR,
    BACKUP_WARN_TOTAL_MB,
)
from src.db import DB_PATH
from src.logger import get_logger

log = get_logger("backup")

STAMP = "%Y%m%d"
SUFFIX = ".db.gz"


@dataclass
class BackupSummary:
    snapshot: Path
    size_bytes: int
    kept: int
    pruned_local: list[str] = field(default_factory=list)
    pruned_offbox: list[str] = field(default_factory=list)
    offbox: Path | None = None
    offbox_error: str | None = None
    total_bytes: int = 0
    over_warn_limit: bool = False


def stem_for(db_path: Path) -> str:
    """``data/brandmonitor.sqlite3`` -> ``brandmonitor``."""
    return db_path.name.split(".")[0]


def snapshot_name(stem: str, day: date) -> str:
    return f"{stem}-{day.strftime(STAMP)}{SUFFIX}"


def parse_day(path: Path, stem: str) -> date | None:
    """Recover the snapshot date from a filename, or None if it is not ours.

    Rotation keys on this rather than on mtime. A file's mtime changes when a
    sync client rewrites it or a restore touches it, and losing the wrong
    snapshot to a touched timestamp is the failure this avoids.
    """
    name = path.name
    if not name.startswith(f"{stem}-") or not name.endswith(SUFFIX):
        return None
    stamp = name[len(stem) + 1: -len(SUFFIX)]
    try:
        return datetime.strptime(stamp, STAMP).date()
    except ValueError:
        return None


def keep_set(days: Iterable[date], daily: int, weekly: int, monthly: int) -> set[date]:
    """Which snapshot dates survive: N most recent, then one per week, per month.

    A date can fill more than one tier - today is the newest daily, the newest
    of its week and the newest of its month at once - so early on the kept count
    is well below ``daily + weekly + monthly`` and grows toward it.
    """
    ordered = sorted(set(days), reverse=True)
    keep: set[date] = set(ordered[:max(daily, 0)])

    for group, limit in (
        (lambda d: d.isocalendar()[:2], weekly),
        (lambda d: (d.year, d.month), monthly),
    ):
        newest: dict[tuple, date] = {}
        for d in ordered:                      # ordered newest-first, so the
            newest.setdefault(group(d), d)     # first hit per bucket is its newest
        for key in sorted(newest, reverse=True)[:max(limit, 0)]:
            keep.add(newest[key])
    return keep


def write_snapshot(db_path: Path, dest_dir: Path, day: date | None = None) -> Path:
    """Online-backup the database, VACUUM the copy, gzip it, return the archive."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    day = day or date.today()
    plain = dest_dir / f"{stem_for(db_path)}-{day.strftime(STAMP)}.db"
    archive = dest_dir / snapshot_name(stem_for(db_path), day)

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(plain))
        try:
            with dst:
                src.backup(dst)
            dst.execute("VACUUM")
        finally:
            dst.close()
    finally:
        src.close()

    try:
        with open(plain, "rb") as fin, gzip.open(archive, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
    finally:
        plain.unlink(missing_ok=True)
    return archive


def prune(directory: Path, stem: str, keep: set[date]) -> list[str]:
    """Delete this app's snapshots whose date is not in ``keep``."""
    removed: list[str] = []
    if not directory.is_dir():
        return removed
    for path in sorted(directory.glob(f"{stem}-*{SUFFIX}")):
        day = parse_day(path, stem)
        if day is None or day in keep:
            continue
        path.unlink()
        removed.append(path.name)
    return removed


def _days_in(directory: Path, stem: str) -> list[date]:
    if not directory.is_dir():
        return []
    days = (parse_day(p, stem) for p in directory.glob(f"{stem}-*{SUFFIX}"))
    return [d for d in days if d is not None]


def run_backup(
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    offbox_dir: Path | None = None,
    day: date | None = None,
    keep_daily: int = BACKUP_KEEP_DAILY,
    keep_weekly: int = BACKUP_KEEP_WEEKLY,
    keep_monthly: int = BACKUP_KEEP_MONTHLY,
    warn_total_mb: int = BACKUP_WARN_TOTAL_MB,
) -> BackupSummary:
    db_path = Path(db_path or DB_PATH)
    backup_dir = Path(backup_dir if backup_dir is not None else BACKUP_DIR)
    if offbox_dir is None:
        offbox_dir = BACKUP_OFFBOX_DIR
    offbox = Path(offbox_dir).expanduser() if offbox_dir else None
    stem = stem_for(db_path)
    day = day or date.today()

    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")

    archive = write_snapshot(db_path, backup_dir, day)
    keep = keep_set(_days_in(backup_dir, stem), keep_daily, keep_weekly, keep_monthly)
    keep.add(day)                                   # never prune what we just wrote
    summary = BackupSummary(
        snapshot=archive, size_bytes=archive.stat().st_size, kept=len(keep),
        pruned_local=prune(backup_dir, stem, keep),
    )

    if offbox is not None:
        # The off-box copy is a plain directory write. On this host that is a
        # OneDrive folder the sync client already pushes, so no rclone remote and
        # no OAuth token is involved. A sync client that is paused or broken must
        # not fail the backup - the local snapshot is already safe on disk.
        try:
            offbox.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, offbox / archive.name)
            summary.offbox = offbox / archive.name
            off_keep = keep_set(_days_in(offbox, stem), keep_daily, keep_weekly,
                                keep_monthly)
            off_keep.add(day)
            summary.pruned_offbox = prune(offbox, stem, off_keep)
        except OSError as exc:
            summary.offbox_error = f"{type(exc).__name__}: {exc}"
            log.warning("off-box copy to %s failed: %s", offbox, exc)

    summary.total_bytes = sum(
        p.stat().st_size for p in backup_dir.glob(f"{stem}-*{SUFFIX}"))
    summary.over_warn_limit = (
        warn_total_mb > 0 and summary.total_bytes > warn_total_mb * 1024 * 1024)
    if summary.over_warn_limit:
        # Warn, never delete. Retention already bounds the normal case, so
        # crossing this line means an assumption broke - and silently deleting
        # more snapshots is the wrong response to not understanding why.
        log.warning("backup directory is %.0f MB, over the %d MB warning limit",
                    summary.total_bytes / 1048576, warn_total_mb)
    log.info("snapshot %s (%.2f MB), %d kept, %d pruned",
             archive.name, summary.size_bytes / 1048576, summary.kept,
             len(summary.pruned_local))
    return summary
