"""Client relevance assessment — STUB.

Deliberately unimplemented. The storage and collection halves of the vertical slice
are well specified by mvp_plan.md; this half is not, and guessing at it would bake
in decisions that need a proper session. See todo.md, "Assessment layer".

What is settled and encoded below:
  * assessments are per client, never stored on the shared raw record;
  * every assessment carries the prompt and profile version that produced it, so a
    result can always be traced back and re-run;
  * analysis has its own watermark, independent of collection.

What is NOT settled, and must not be guessed:
  * the prompt itself, and whether relevance and severity are one call or two;
  * whether a keyword prefilter runs before the LLM (probably yes — most swept
    articles mention no client brand, and paying an LLM to say so is waste);
  * the model, and whether Doubao stays the default here;
  * the structured output schema the report layer will need;
  * how a client profile is versioned when brands are added mid-cycle.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.db import session, utcnow
from src.logger import get_logger

logger = get_logger(__name__)

PROMPT_VERSION = "unimplemented-0"
PROFILE_VERSION = "unimplemented-0"


class NotImplementedYet(RuntimeError):
    """Raised so a caller fails loudly rather than silently storing nothing."""


def load_client_profile(client_slug: str, clients_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Read clients/<slug>/profile.json. Shape is not fixed yet."""
    base = Path(clients_dir) if clients_dir else Path("clients")
    path = base / client_slug / "profile.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no client profile at {path}. Expected brands, aliases, categories, "
            "sourcing countries and EU legal role - see mvp_plan.md section 5."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def unassessed_items(conn: sqlite3.Connection, client_slug: str,
                     limit: int = 100) -> List[sqlite3.Row]:
    """Raw items with no assessment for this client at the current versions.

    This query is real and useful on its own — it is how the analysis watermark
    stays independent of collection.
    """
    # Order by published_at, falling back to fetched_at. Some feeds publish undated
    # entries (all 27 Bundesnetzagentur items, for one), and SQLite sorts NULLs last
    # under DESC — so without the fallback those items starve whenever a LIMIT bites
    # and would never be assessed at all.
    return list(conn.execute(
        "SELECT r.* FROM raw_item r "
        "LEFT JOIN assessment a ON a.raw_item_id = r.id "
        "  AND a.client_slug = ? AND a.prompt_version = ? AND a.profile_version = ? "
        "WHERE a.id IS NULL "
        "ORDER BY COALESCE(r.published_at, r.fetched_at) DESC LIMIT ?",
        (client_slug, PROMPT_VERSION, PROFILE_VERSION, limit),
    ))


def store_assessment(conn: sqlite3.Connection, raw_item_id: int, client_slug: str,
                     relevant: Optional[bool], payload: Dict[str, Any]) -> None:
    """Write one assessment, tagged with the versions that produced it."""
    conn.execute(
        "INSERT OR REPLACE INTO assessment "
        "(raw_item_id, client_slug, prompt_version, profile_version, created_at, "
        " relevant, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (raw_item_id, client_slug, PROMPT_VERSION, PROFILE_VERSION, utcnow(),
         None if relevant is None else int(relevant),
         json.dumps(payload, ensure_ascii=False)),
    )


def assess_item(item: sqlite3.Row, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Assess one raw item against one client profile. NOT IMPLEMENTED."""
    raise NotImplementedYet(
        "The relevance prompt is not designed yet. See todo.md, 'Assessment layer'."
    )


def run_assessment(client_slug: str, limit: int = 100,
                   db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Assess pending items for one client. NOT IMPLEMENTED."""
    with session(db_path) as conn:
        pending = unassessed_items(conn, client_slug, limit)
    raise NotImplementedYet(
        f"{len(pending)} item(s) are waiting for client '{client_slug}', but the "
        "assessment layer is a stub. See todo.md."
    )
