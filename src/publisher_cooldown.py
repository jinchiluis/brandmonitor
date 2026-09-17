"""Short publisher pauses, with seven days of history for repeated rejections.

Readers need no lock: writers replace the JSON atomically. A separate OS lock
serializes the complete read/modify/write, including overlapping manual runs.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from health.common import atomic_write_json, format_utc, now_utc
from src.logger import get_logger

logger = get_logger(__name__)
COOLDOWN = timedelta(hours=23)
HISTORY = timedelta(days=7)
ESCALATE_DAYS = 3


def path_for(db_path: Path | None = None) -> Path:
    from src.db import DB_PATH

    return Path(db_path or DB_PATH).parent / "publisher_cooldown.json"


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cooldown timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def read(path: Path, known_slugs: set[str] | None = None) -> dict[str, dict]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(entries, dict):
        raise ValueError(f"publisher cooldown file must contain an object: {path}")
    result = {}
    for slug, entry in entries.items():
        if known_slugs is not None and slug not in known_slugs:
            logger.warning("Ignoring publisher cooldown outside configured source list: %s", slug)
            continue
        try:
            for key in ("since", "until", "last_rejected_at"):
                timestamp(entry[key])
            if (type(entry["consecutive_days"]) is not int or entry["consecutive_days"] < 1
                    or type(entry["status"]) is not int
                    or entry["stage"] not in ("collect", "bodies")
                    or not isinstance(entry["reason"], str)):
                raise ValueError("invalid rejection fields")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError(f"invalid publisher cooldown entry for {slug}: {exc}") from exc
        result[slug] = entry
    return result


def active(path: Path, known_slugs: set[str], *, now: datetime | None = None) -> dict[str, dict]:
    at = now or now_utc()
    return {slug: entry for slug, entry in read(path, known_slugs).items()
            if timestamp(entry["until"]) > at}


@contextmanager
def _write_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def record(path: Path, slug: str, *, stage: str, status: int, reason: str,
           retry_after: float | None = None, now: datetime | None = None) -> dict:
    at = now or now_utc()
    until = at + max(COOLDOWN, timedelta(seconds=retry_after or 0))
    with _write_lock(path):
        entries = read(path)
        previous = entries.get(slug)
        days, since = 1, format_utc(at)
        if previous:
            gap = (at.date() - timestamp(previous["last_rejected_at"]).date()).days
            if gap in (0, 1):
                days = previous["consecutive_days"] + gap
                since = previous["since"]
            until = max(until, timestamp(previous["until"]))
        # Expiry permits traffic again; it does not erase yesterday's history.
        entries = {s: e for s, e in entries.items()
                   if timestamp(e["until"]) > at
                   or timestamp(e["last_rejected_at"]) > at - HISTORY}
        entry = {"until": format_utc(until), "since": since,
                 "last_rejected_at": format_utc(at), "reason": reason,
                 "stage": stage, "status": status, "consecutive_days": days}
        entries[slug] = entry
        atomic_write_json(path, entries)
    logger.warning("[publisher cooldown] %s until %s: %s", slug, entry["until"], reason)
    return entry
