"""A frozen export on disk, and the small reader every later stage shares.

A bundle is a directory of JSON files written once by ``export-window`` and
never modified afterwards. Stages 2-4 read it; nothing but stage 1 writes the
frozen half. Keeping the reader here means the assessor, the renderer and the
verifier cannot disagree about what "the evidence" is.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.config import DATA_DIR

REPORTS_DIR = DATA_DIR / "reports"

# Windows are named in the client's business timezone and stored in UTC. The
# customer's week is a German week; storing the bounds in UTC keeps them
# comparable with fetched_at, which is UTC everywhere in this database.
DISPLAY_TZ = ZoneInfo("Europe/Berlin")

# Files stage 1 writes. Stage 4 checks they are all present before trusting a
# bundle, so a half-written export fails loudly rather than reporting on part
# of a week.
FROZEN_FILES = (
    "manifest.json",
    "evidence.json",
    "stopped_items.json",
    "gate_census.json",
    "post_cutoff_gate_items.json",
    "title_routes.json",
    "unavailable_title_evidence.json",
    "dip_documents.json",
    "collection_runs.json",
    "source_runs.json",
    "coverage.json",
    "client_profile.json",
)


def window_bounds(since: date, until: date) -> tuple[str, str]:
    """(start, end-exclusive) as UTC ISO strings for an inclusive local day range.

    ``until`` is inclusive because that is how a customer reads "5 through 11";
    the exclusive bound the queries need is midnight after it.
    """
    if until < since:
        raise ValueError(f"window ends before it starts: {since}..{until}")
    start = datetime.combine(since, datetime.min.time(), DISPLAY_TZ)
    end = datetime.combine(until + timedelta(days=1), datetime.min.time(), DISPLAY_TZ)
    return (start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat())


def default_bundle_dir(client_slug: str, since: date, until: date) -> Path:
    return REPORTS_DIR / f"{client_slug}-{since.isoformat()}_{until.isoformat()}"


class BundleError(RuntimeError):
    """A bundle is missing, incomplete, or not the one the caller meant."""


class Bundle:
    """Read access to one frozen export directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise BundleError(f"no bundle directory at {self.path}")
        if not (self.path / "manifest.json").exists():
            raise BundleError(f"{self.path} has no manifest.json; not a bundle")

    # -- reading --------------------------------------------------------

    def load(self, name: str) -> Any:
        path = self.path / name
        if not path.exists():
            raise BundleError(f"{self.path.name} is missing {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def maybe(self, name: str, default: Any = None) -> Any:
        """Read an optional file - a stage that has not run yet, usually."""
        path = self.path / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, obj: Any) -> Path:
        path = self.path / name
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.path / name
        path.write_text(text, encoding="utf-8")
        return path

    def missing_frozen(self) -> list[str]:
        return [name for name in FROZEN_FILES if not (self.path / name).exists()]

    # -- the frozen material --------------------------------------------

    @cached_property
    def manifest(self) -> dict[str, Any]:
        return self.load("manifest.json")

    @cached_property
    def evidence(self) -> list[dict[str, Any]]:
        return self.load("evidence.json")

    @cached_property
    def stopped(self) -> list[dict[str, Any]]:
        return self.load("stopped_items.json")

    @cached_property
    def unavailable(self) -> list[dict[str, Any]]:
        """Titles routed for a body fetch whose body never arrived."""
        return self.maybe("unavailable_title_evidence.json", [])

    @cached_property
    def by_id(self) -> dict[int, dict[str, Any]]:
        """Every identity in the export: candidates, stopped, and unreadable.

        Stage 2 is allowed to read a stopped item - an open issue must be able
        to search material the gate rejected (report_plan.md §2.3). It is also
        allowed to name a title whose body never arrived, because "we saw this
        and could not read it" is a reportable fact. What it may not do is
        invent an identity, which is why every tool resolves through here.
        """
        return {row["id"]: row
                for row in self.evidence + self.stopped + self.unavailable}

    @cached_property
    def client_slug(self) -> str:
        return self.manifest["client"]

    @cached_property
    def window_end(self) -> str:
        return self.manifest["window_end_exclusive"]

    @cached_property
    def display_cutoff(self) -> str:
        """The cutoff as the customer reads it: local wall-clock, not UTC.

        Stored UTC so it compares with fetched_at; shown in Berlin time because
        "information cutoff 22:00" reads as an hour before midnight to a reader
        in Germany, and the cutoff is in fact midnight.
        """
        moment = datetime.fromisoformat(self.window_end).astimezone(DISPLAY_TZ)
        return moment.strftime("%Y-%m-%d %H:%M Europe/Berlin")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Bundle {self.path.name}>"


def previous_bundle(client_slug: str, window_start: str,
                    reports_dir: Path | None = None) -> Bundle | None:
    """The client's most recent bundle that closed before this window opened.

    File-based rather than a database table, deliberately for now: a cycle's
    register is a dated artefact that diffs, and the report table holds one row
    per delivered report rather than per assessment run. report_plan.md §8
    leaves this open; moving it into SQLite later is a reader change here and
    nothing else.
    """
    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    if not root.is_dir():
        return None
    candidates: list[tuple[str, Bundle]] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("client") != client_slug:
            continue
        end = manifest.get("window_end_exclusive") or ""
        if not end or end > window_start:
            continue
        if not (manifest_path.parent / "issue-register.json").exists():
            continue
        candidates.append((end, Bundle(manifest_path.parent)))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]
