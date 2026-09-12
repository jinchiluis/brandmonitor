"""Shared file helpers for the laptop-side health observers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace *path* with one complete UTF-8 JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish_snapshot(
    output_dir: Path, cycle_id: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    """Publish an immutable first observation plus an atomically replaced latest."""
    safe_cycle = re.sub(r"[^A-Za-z0-9_.-]+", "-", cycle_id).strip("-.")
    if not safe_cycle:
        raise ValueError("cycle_id contains no usable filename characters")
    history = output_dir / "history" / f"{safe_cycle}.json"
    latest = output_dir / "latest.json"
    # A manual rerun can observe a website later in the day. Preserve the first
    # observation associated with the database run; latest may still be refreshed.
    if not history.exists():
        atomic_write_json(history, payload)
    atomic_write_json(latest, payload)
    return history, latest
