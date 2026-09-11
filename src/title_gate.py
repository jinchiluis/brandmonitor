"""Title gate: a cheap LLM check on title-only candidates before a body fetch.

The keyword selector is high-recall on purpose, so on the title-only tier most of
what it selects is noise: measured 2026-09-11, 23 of 247 candidates were worth
reading. Fetching and fully assessing the rest would spend paid accounts and the
expensive model on China-policy headlines and share-price notes. This stage
sends each headline - or the slug, where a publisher ships no title - to a small
model in numbered batches and keeps the numbers it names.

Only candidates without a stored body are gated. A body is evidence already, so
those go straight to the full assessment; regulatory records never come here,
because their relevance is topical.

The prompt has two halves. ``src/prompts/title_gate.md`` is the same for every
client. The client's description, what counts as relevant and its measured false
matches come from ``prompt`` in ``clients/<slug>/profile.json``, and brand names
from its brand rules by role - so a new client is a profile, not code.

The reply is numbers only, and "0" means none. An empty, wordy or out-of-range
reply is therefore an error rather than a silent "drop everything": it is retried
once, and a batch that still fails is kept whole. Dropping is final for a
title-only item - its body is never fetched and nothing downstream sees it - so
every failure has to fall towards keeping.

Each decision, kept or dropped, is appended to
``data/title_gate/<client>/<date>.jsonl`` with the prompt, profile and model
versions. Dropped items appear nowhere else, so that log is the only place a
wrong drop can be spotted. It is working data, pruned after ``log_keep_days``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Collection, Sequence

from src.config import (DATA_DIR, ROOT, TITLE_GATE_BATCH_SIZE, TITLE_GATE_LOG_KEEP_DAYS,
                        TITLE_GATE_MODEL, TITLE_GATE_REASONING, TITLE_GATE_TIMEOUT)
from src.logger import get_logger
from src.selector import Candidate, selection_from_db, url_metadata
from src.profile import ClientProfile

logger = get_logger(__name__)

PROMPT_PATH = ROOT / "src" / "prompts" / "title_gate.md"
LOG_ROOT = DATA_DIR / "title_gate"

# (system prompt, numbered items) -> (reply text, {"in": tokens, "out": tokens}).
# The model call is passed in, so tests run the whole gate without a network.
Caller = Callable[[str, str], tuple[str, dict]]

REPLY_SHAPE = re.compile(r"[\d\s,]+")


class GateConfigError(RuntimeError):
    """The gate cannot run at all: no key, unknown model, profile without prompt.

    Every batch would fail the same way, and keeping everything by default would
    make that look like a quiet day, so this aborts instead.
    """


@dataclass
class GateResult:
    kept: list[Candidate] = field(default_factory=list)
    dropped: list[Candidate] = field(default_factory=list)
    batches: int = 0
    failed_batches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_version: str = ""
    log_path: Path | None = None


# ── prompt ────────────────────────────────────────────────────────────────

def brand_sentence(profile: ClientProfile) -> str:
    """Name the client, its competitors and its customers from the brand rules."""
    parts = []
    own = profile.brands_with_role("own")
    if own:
        aliases = f" ({', '.join(own[1:])})" if own[1:] else ""
        parts.append(f"{own[0]}{aliases} is the client.")
    for role, label in (("competitor", "Competitors"), ("customer", "Customers")):
        names = profile.brands_with_role(role)
        if names:
            parts.append(f"{label}: {', '.join(names)}.")
    others = [name for name, role in profile.brands
              if role not in ("own", "competitor", "customer")]
    if others:
        parts.append(f"Also tracked: {', '.join(others)}.")
    return " ".join(parts)


def render_system_prompt(profile: ClientProfile, template: str | None = None) -> str:
    if profile.prompt is None:
        raise GateConfigError(
            f"profile {profile.slug} has no 'prompt' section; the title gate needs "
            "its about, relevant and false_matches")
    text = PROMPT_PATH.read_text(encoding="utf-8") if template is None else template
    values = {
        "name": profile.name,
        "about": profile.prompt.about,
        "brands": brand_sentence(profile),
        "relevant": "\n".join(f"- {line}" for line in profile.prompt.relevant),
        "false_matches": "\n".join(f"- {line}" for line in profile.prompt.false_matches)
                         or "- none listed",
    }
    # Plain replacement rather than str.format, so a brace an editor adds to the
    # template is text, not a KeyError at six in the morning.
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    unknown = sorted(set(re.findall(r"\{(\w+)\}", text)))
    if unknown:
        raise GateConfigError(f"{PROMPT_PATH.name} has unknown placeholders: {unknown}")
    return text.strip()


def prompt_version(system: str) -> str:
    """Identify the exact prompt text, so no edit can go unrecorded."""
    return "title_gate-" + hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]


def item_label(candidate: Candidate) -> str:
    return candidate.title or url_metadata(candidate.url)[0] or candidate.url


def render_batch(batch: Sequence[Candidate]) -> str:
    """One numbered line per item: the rules it matched, its source, its label.

    The matched rules turn the task into "is this match real?", which is the
    question a small model answers well - Hermes the carrier or the fashion house.
    """
    lines = []
    for n, candidate in enumerate(batch, 1):
        keywords = ", ".join(reason.split(":", 1)[-1] for reason in candidate.reasons)
        lines.append(f"{n}. [{keywords}] {candidate.source_slug}: {item_label(candidate)}")
    return "\n".join(lines)


def parse_reply(text: str, n: int) -> list[int] | None:
    """Positions to keep (1-based), [] for "0", or None for an unusable reply.

    Strict on purpose. A reply with words in it, or a number that names no
    item, means the model did not do what it was asked, and reading numbers out
    of it anyway could drop an item on the strength of a date or a count.
    """
    stripped = text.strip()
    if not stripped or not REPLY_SHAPE.fullmatch(stripped):
        return None
    numbers = [int(token) for token in re.findall(r"\d+", stripped)]
    if numbers == [0]:
        return []
    if any(not 1 <= number <= n for number in numbers):
        return None
    return sorted(set(numbers))


# ── model ─────────────────────────────────────────────────────────────────

def openai_caller(model: str = TITLE_GATE_MODEL, effort: str = TITLE_GATE_REASONING,
                  timeout: float = TITLE_GATE_TIMEOUT,
                  text_format: dict | None = None) -> Caller:
    """The production caller. Reasoning stays on: measured, "none" cost mini 14
    missed items across three runs where "low" cost one.

    ``text_format`` is passed through as the Responses API ``text.format``; the
    body gate uses it for a JSON-schema reply. The title gate's reply is plain
    numbers and needs none.
    """
    from dotenv import load_dotenv

    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise GateConfigError("OPENAI_API_KEY is not set; it belongs in .env on the laptop")
    import openai

    client = openai.OpenAI(timeout=timeout, max_retries=3)
    options = {"text": {"format": text_format}} if text_format else {}

    def call(system: str, user: str) -> tuple[str, dict]:
        try:
            response = client.responses.create(
                model=model, instructions=system, input=user,
                reasoning={"effort": effort}, **options)
        except (openai.AuthenticationError, openai.PermissionDeniedError,
                openai.NotFoundError, openai.BadRequestError) as exc:
            raise GateConfigError(f"{type(exc).__name__}: {exc}") from exc
        usage = response.usage
        return response.output_text or "", {"in": usage.input_tokens,
                                            "out": usage.output_tokens}

    return call


# ── gate ──────────────────────────────────────────────────────────────────

def gate_candidates(profile: ClientProfile, db_path: Path, sources_path: Path,
                    first_seen_in: Collection[int] | None
                    ) -> tuple[list[Candidate], list[Candidate]]:
    """Title-only candidates split into (to gate, already have a body)."""
    result = selection_from_db(db_path, ((sources_path, "news"),), "title-only",
                               profile, first_seen_in=first_seen_in)
    to_gate = [c for c in result.candidates if not c.has_body]
    with_body = [c for c in result.candidates if c.has_body]
    return to_gate, with_body


def run_gate(candidates: Sequence[Candidate], profile: ClientProfile, caller: Caller, *,
             model: str = TITLE_GATE_MODEL, batch_size: int = TITLE_GATE_BATCH_SIZE,
             run_id: int | None = None, log_root: Path | None = None,
             keep_days: int = TITLE_GATE_LOG_KEEP_DAYS) -> GateResult:
    system = render_system_prompt(profile)
    result = GateResult(prompt_version=prompt_version(system))
    log_dir = (log_root or LOG_ROOT) / profile.slug
    if not candidates:
        prune_logs(log_dir, keep_days)
        return result

    log_dir.mkdir(parents=True, exist_ok=True)
    result.log_path = log_dir / f"{date.today().isoformat()}.jsonl"
    with result.log_path.open("a", encoding="utf-8") as log:
        for start in range(0, len(candidates), batch_size):
            batch = list(candidates[start:start + batch_size])
            result.batches += 1
            keep, reply, error = _decide(system, batch, caller, result)
            fail_open = keep is None
            if fail_open:
                result.failed_batches += 1
                keep = list(range(1, len(batch) + 1))
                logger.warning("[gate] batch %d kept whole after two unusable replies: %s",
                               result.batches, error or repr(reply[:80]))
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for n, candidate in enumerate(batch, 1):
                kept = n in keep
                (result.kept if kept else result.dropped).append(candidate)
                log.write(json.dumps({
                    "at": at, "client": profile.slug,
                    "profile_version": profile.profile_version,
                    "prompt_version": result.prompt_version, "model": model,
                    "run_id": run_id, "batch": result.batches,
                    "source": candidate.source_slug, "external_id": candidate.external_id,
                    "url": candidate.url, "label": item_label(candidate),
                    "reasons": list(candidate.reasons),
                    "decision": "keep" if kept else "drop", "fail_open": fail_open,
                    "reply": reply, "error": error,
                }, ensure_ascii=False) + "\n")
    prune_logs(log_dir, keep_days)
    return result


def _decide(system: str, batch: list[Candidate], caller: Caller,
            result: GateResult) -> tuple[list[int] | None, str, str | None]:
    """Ask twice at most. Returns (positions or None, last reply, last error)."""
    user = render_batch(batch)
    reply, error = "", None
    for attempt in (1, 2):
        try:
            reply, usage = caller(system, user)
            error = None
        except GateConfigError:
            raise
        except Exception as exc:  # a failed batch must not stop the others
            reply, usage, error = "", {}, f"{type(exc).__name__}: {exc}"[:300]
        result.input_tokens += usage.get("in", 0)
        result.output_tokens += usage.get("out", 0)
        keep = None if error else parse_reply(reply, len(batch))
        if keep is not None:
            return keep, reply, None
        logger.info("[gate] batch %d attempt %d unusable: %s", result.batches, attempt,
                    error or repr(reply[:80]))
    return None, reply, error


def prune_logs(log_dir: Path, keep_days: int, today: date | None = None) -> list[Path]:
    """Delete decision logs older than keep_days. Returns what was removed."""
    if not log_dir.exists():
        return []
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    removed = []
    for path in log_dir.glob("*.jsonl"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue  # not ours; leave it
        if day < cutoff:
            path.unlink()
            removed.append(path)
    return removed
