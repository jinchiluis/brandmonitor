"""Body gate: a cheap LLM relevance check between a stored body and the full assessment.

The keyword selector is high-recall, so most bodies it passes are not signal for
the client: measured 2026-09-11, 4 of 30 random picks were relevant, and 1 of the
22 that matched on keywords alone. This stage asks a small model one question per
body - is this a signal for the client - and gets relevant, unsure or irrelevant
with a short reason. Only irrelevant stops an item; unsure goes on to the full
assessment. Separating the whether from the what keeps the expensive prompt from
inventing a severity for a page that is not about the client, and a keep/drop
decision can be scored against hand labels where a full assessment cannot
(docs/selection_and_assessment.md, "Body gate").

Two prompts, one per tier. News bodies are shown with the rules they matched, so
the question becomes "is this match real?" - the question a small model answers
well. Regulatory bodies are shown without them: their relevance is topical, so
that prompt lists legal areas instead of business topics. The templates in
``src/prompts/`` are shared by every client; what the client is, its areas and
its measured false matches come from ``prompt`` in its profile.

The input is stored bodies with no body-gate decision yet, newest first. Full-text
news and regulatory bodies come through the selector. A body carrying this client's
title-gate route skips this stage: the title gate already made the cheap relevance
decision, so after enrichment it goes directly to the full assessor. An item is
identified by source and external id, not by version: a restamp writes version 2 of
an old URL and is not a new article. A profile or prompt change therefore applies
to new items; ``regate_from`` re-offers items decided under another version,
deliberately.

Decisions are stored in ``assessment`` - relevant 1, unsure NULL, irrelevant 0 -
under a prompt version ``body_gate-<kind>-<hash>``, with the verdict, reason,
model and selector reasons in the payload. Stored rather than logged: the full
assessment reads them, and can fail and re-run without re-gating.

Failure falls towards keeping. An unusable reply is retried once, and an item
that still fails is stored as unsure with ``fail_open`` set. Three failed items in
a row stop the run and leave the rest ungated for the next one, so an outage
costs a handful of fail-open items rather than flooding the full assessment. A
configuration error - no key, unknown model, a profile without the inputs -
aborts at once.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from src.collect import DEFAULT_NEWS_SOURCES, DEFAULT_REGULATORY_SOURCES
from src.config import (BODY_GATE_BODY_CHARS, BODY_GATE_MODEL, BODY_GATE_REASONING,
                        BODY_GATE_TIMEOUT, BODY_GATE_WORKERS, ROOT)
from src.db import connect, utcnow
from src.logger import get_logger
from src.profile import ClientProfile
from src.selector import Candidate, selection_from_db, url_metadata
from src.title_gate import Caller, GateConfigError, brand_sentence
from src.title_gate import openai_caller as _openai_caller

logger = get_logger(__name__)

KINDS = ("news", "regulatory")
SOURCES = {"news": DEFAULT_NEWS_SOURCES, "regulatory": DEFAULT_REGULATORY_SOURCES}
PROMPT_DIR = ROOT / "src" / "prompts"
TEMPLATES = {"news": PROMPT_DIR / "body_gate.md",
             "regulatory": PROMPT_DIR / "body_gate_regulatory.md"}
STAGE = "body_gate"

# Verdict -> assessment.relevant. Unsure is NULL, the column's "undecided".
VERDICTS = {"relevant": 1, "unsure": None, "irrelevant": 0}

MAX_FAILURES_IN_A_ROW = 3

REPLY_FORMAT = {
    "type": "json_schema", "name": "body_gate", "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            # Reason first, so the verdict follows from it rather than the reverse.
            "reason": {"type": "string"},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
        },
        "required": ["reason", "verdict"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class GateItem:
    """A selected candidate, the body the gate reads, and the row that body is on."""
    candidate: Candidate
    raw_item_id: int
    body: str


@dataclass
class Decision:
    item: GateItem
    verdict: str
    reason: str
    fail_open: bool = False
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class BodyGateResult:
    kind: str
    prompt_version: str = ""
    decisions: list[Decision] = field(default_factory=list)
    # Why the run stopped before gating every item it was given, if it did.
    stopped: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def count(self, verdict: str) -> int:
        return sum(1 for d in self.decisions if d.verdict == verdict)

    @property
    def fail_open(self) -> int:
        return sum(1 for d in self.decisions if d.fail_open)


# ── prompt ────────────────────────────────────────────────────────────────

def _bullets(lines: Sequence[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def render_system_prompt(profile: ClientProfile, kind: str,
                         template: str | None = None) -> str:
    prompt = profile.prompt
    if prompt is None:
        raise GateConfigError(f"profile {profile.slug} has no 'prompt' section; the body "
                              "gate needs its about, relevant and false_matches")
    if kind == "news":
        values = {"relevant": _bullets(prompt.relevant),
                  "false_matches": _bullets(prompt.false_matches + prompt.body_false_matches)
                  or "- none listed"}
    elif kind == "regulatory":
        if not prompt.regulatory_relevant:
            raise GateConfigError(
                f"profile {profile.slug} has no prompt.regulatory_relevant; the regulatory "
                "body gate needs the legal areas that matter to the client")
        values = {"regulatory_relevant": _bullets(prompt.regulatory_relevant),
                  "regulatory_false_matches": _bullets(prompt.regulatory_false_matches)}
    else:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    values.update(name=profile.name, about=prompt.about, brands=brand_sentence(profile))

    text = TEMPLATES[kind].read_text(encoding="utf-8") if template is None else template
    # Plain replacement rather than str.format, so a brace an editor adds to the
    # template is text, not a KeyError at six in the morning. An empty list drops
    # its placeholder line instead of leaving a blank bullet.
    for key, value in values.items():
        if not value:
            text = text.replace("{" + key + "}\n", "")
        text = text.replace("{" + key + "}", value)
    unknown = sorted(set(re.findall(r"\{(\w+)\}", text)))
    if unknown:
        raise GateConfigError(f"{TEMPLATES[kind].name} has unknown placeholders: {unknown}")
    return text.strip()


def prompt_version(kind: str, system: str) -> str:
    """Name the stage and tier, and identify the exact prompt text."""
    return f"{STAGE}-{kind}-" + hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]


def item_label(candidate: Candidate) -> str:
    return candidate.title or url_metadata(candidate.url)[0] or candidate.url


def render_item(item: GateItem, kind: str, body_chars: int = BODY_GATE_BODY_CHARS) -> str:
    """Source, title and the start of the body; news also gets its matched rules."""
    candidate = item.candidate
    head = f"{candidate.source_slug}\nTITLE: {item_label(candidate)}"
    if kind == "news":
        keywords = ", ".join(reason.split(":", 1)[-1] for reason in candidate.reasons)
        head = f"[{keywords}] {head}"
    return f"{head}\n\n{item.body[:body_chars]}"


def parse_reply(text: str) -> tuple[str, str] | None:
    """(verdict, reason), or None for anything that is not the schema's shape."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict, reason = data.get("verdict"), data.get("reason")
    if verdict not in VERDICTS or not isinstance(reason, str):
        return None
    return verdict, reason.strip()


# ── model ─────────────────────────────────────────────────────────────────

def openai_caller(model: str = BODY_GATE_MODEL, effort: str = BODY_GATE_REASONING,
                  timeout: float = BODY_GATE_TIMEOUT) -> Caller:
    """The title gate's caller, asking for the body gate's JSON reply."""
    return _openai_caller(model=model, effort=effort, timeout=timeout,
                          text_format=REPLY_FORMAT)


# ── input ─────────────────────────────────────────────────────────────────

def pending_items(profile: ClientProfile, kind: str, db_path: Path,
                  sources_path: Path | None = None, *,
                  regate_from: str | None = None) -> tuple[list[GateItem], int]:
    """Selected candidates with a stored body and no decision yet, newest first.

    Returns (items, number already gated). Any earlier body-gate decision for
    the same tier counts, whatever its version; with ``regate_from`` only a
    decision under that prompt version and the current profile version does.
    """
    selection = selection_from_db(db_path, ((sources_path or SOURCES[kind], kind),),
                                  "all", profile)
    candidates = {(c.source_slug, c.external_id): c for c in selection.candidates
                  if c.has_body}

    prefix = f"{STAGE}-{kind}-"
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in conn.execute(
                "SELECT id, source_slug, external_id, url, title, published_at, fetched_at, payload "
                "FROM raw_item WHERE source_kind = ? ORDER BY source_slug, external_id, version",
                (kind,)):
            latest[(row["source_slug"], row["external_id"])] = row
        if kind == "news":
            # A title-gate keep is already client-approved. Its queue route is the
            # durable handoff to the full assessor, so asking the body gate again
            # would pay for a second cheap relevance decision and could contradict
            # the first one merely because the fetched body uses different words.
            for task in conn.execute(
                    "SELECT source_slug, external_id, hint_payload FROM body_fetch "
                    "WHERE status='ok'"):
                key = (task["source_slug"], task["external_id"])
                try:
                    route = json.loads(task["hint_payload"]).get(
                        "title_gate_routes", {}).get(profile.slug)
                except (TypeError, ValueError):
                    continue
                if route:
                    candidates.pop(key, None)
        query = ("SELECT DISTINCT r.source_slug, r.external_id FROM assessment a "
                 "JOIN raw_item r ON r.id = a.raw_item_id "
                 "WHERE a.client_slug = ? AND r.source_kind = ? "
                 "AND substr(a.prompt_version, 1, ?) = ?")
        params: list = [profile.slug, kind, len(prefix), prefix]
        if regate_from:
            query += " AND a.prompt_version = ? AND a.profile_version = ?"
            params += [regate_from, profile.profile_version]
        gated = {(r["source_slug"], r["external_id"]) for r in conn.execute(query, params)}
    finally:
        conn.close()

    ranked: list[tuple[str, GateItem]] = []
    already = 0
    for candidate in candidates.values():
        key = (candidate.source_slug, candidate.external_id)
        if key in gated:
            already += 1
            continue
        row = latest[key]
        try:
            body = json.loads(row["payload"]).get("body_text") or ""
        except (TypeError, ValueError):
            body = ""
        # Undated rows sort by fetch time; sorting NULLs last would starve them
        # whenever the limit bites, and regulators are mostly undated.
        ranked.append((row["published_at"] or row["fetched_at"] or "",
                       GateItem(candidate, row["id"], body)))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _when, item in ranked], already


# ── gate ──────────────────────────────────────────────────────────────────

def run_body_gate(items: Sequence[GateItem], profile: ClientProfile, kind: str,
                  caller: Caller, *, db_path: Path, model: str = BODY_GATE_MODEL,
                  effort: str = BODY_GATE_REASONING, workers: int = BODY_GATE_WORKERS,
                  body_chars: int = BODY_GATE_BODY_CHARS) -> BodyGateResult:
    """Gate items and store every decision as it arrives."""
    system = render_system_prompt(profile, kind)
    result = BodyGateResult(kind=kind, prompt_version=prompt_version(kind, system))
    if not items:
        return result

    workers = max(1, workers)
    queue = iter(items)
    running: set[Future] = set()
    failures_in_a_row = 0
    conn = connect(db_path)
    pool = ThreadPoolExecutor(max_workers=workers)

    def top_up() -> None:
        # Submitted lazily, so a stop leaves the rest unsent rather than queued.
        while len(running) < workers:
            item = next(queue, None)
            if item is None:
                return
            running.add(pool.submit(_decide, system, item, kind, caller, body_chars))

    try:
        top_up()
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                running.discard(future)
                decision = future.result()  # a GateConfigError ends the run here
                _store(conn, decision, profile, kind, result.prompt_version, model, effort)
                conn.commit()
                result.decisions.append(decision)
                result.input_tokens += decision.input_tokens
                result.output_tokens += decision.output_tokens
                failures_in_a_row = failures_in_a_row + 1 if decision.fail_open else 0
                if failures_in_a_row >= MAX_FAILURES_IN_A_ROW and result.stopped is None:
                    result.stopped = (f"{failures_in_a_row} items in a row failed, last: "
                                      f"{decision.error}")
                    logger.warning("[body gate] %s: stopping, %s", kind, result.stopped)
            if result.stopped is None:
                top_up()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        conn.close()
    return result


def _decide(system: str, item: GateItem, kind: str, caller: Caller,
            body_chars: int) -> Decision:
    """Ask twice at most. A second failure is stored as a fail-open unsure."""
    user = render_item(item, kind, body_chars)
    reply, error = "", None
    tokens_in = tokens_out = 0
    for attempt in (1, 2):
        try:
            reply, usage = caller(system, user)
            error = None
        except GateConfigError:
            raise
        except Exception as exc:  # one failed item must not stop the others
            reply, usage, error = "", {}, f"{type(exc).__name__}: {exc}"[:300]
        tokens_in += usage.get("in", 0)
        tokens_out += usage.get("out", 0)
        parsed = None if error else parse_reply(reply)
        if parsed is not None:
            verdict, reason = parsed
            return Decision(item, verdict, reason, input_tokens=tokens_in,
                            output_tokens=tokens_out)
        logger.info("[body gate] %s attempt %d unusable: %s", item.candidate.url, attempt,
                    error or repr(reply[:80]))
    return Decision(item, "unsure", "", fail_open=True,
                    error=error or f"unusable reply: {reply[:200]!r}",
                    input_tokens=tokens_in, output_tokens=tokens_out)


def _store(conn: sqlite3.Connection, decision: Decision, profile: ClientProfile, kind: str,
           version: str, model: str, effort: str) -> None:
    candidate = decision.item.candidate
    payload = {
        "stage": STAGE, "kind": kind, "verdict": decision.verdict, "reason": decision.reason,
        "model": model, "reasoning_effort": effort,
        "selector_reasons": list(candidate.reasons),
        "fail_open": decision.fail_open, "error": decision.error,
        "tokens": {"in": decision.input_tokens, "out": decision.output_tokens},
    }
    conn.execute(
        "INSERT OR REPLACE INTO assessment (raw_item_id, client_slug, prompt_version, "
        "profile_version, created_at, relevant, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (decision.item.raw_item_id, profile.slug, version, profile.profile_version,
         utcnow(), VERDICTS[decision.verdict], json.dumps(payload, ensure_ascii=False)))
