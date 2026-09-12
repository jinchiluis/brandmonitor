"""Stage 2: the only stage with judgment in it.

Six steps over a frozen bundle, with state on disk rather than in a preserved
conversation. Passing a growing transcript between steps would break four
project rules at once: decisions must be traceable to a prompt version, the
stage must be re-runnable, stage 4 can only verify keyed rows, and one story's
framing must not bleed into the next.

    1 carry-forward   agentic   what happened to last cycle's open issues,
                                searching the gate's rejects as well
    2 triage          fixed     which candidates are worth an analyst's time
    3 cluster         fixed     which of those describe one development
    4 deep read       agentic   one story at a time, reading bodies
    5 challenge       agentic   an adversarial pass over each draft
    6 write           fixed     the Chinese report, from the register only

Steps 1, 4 and 5 are genuinely agentic: they decide what to read next from what
they just read. The port recovery is the canonical case - a September article
about a continuing dispute is only resolvable by searching back for the August
one, among items a gate stopped.

What this stage does *not* do is spend a model on bulk bookkeeping. 900-odd of
the 1,000 identities in a week get their ledger row from a rule; the assessor
decides the shortlist and the issues, and the renderer joins the two.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.logger import get_logger
from src.profile import ClientProfile, load_profile
from src.report_agent.bundle import Bundle
from src.report_agent.llm import Assessor, AssessorError
from src.report_agent.schema import (RULE_TREATMENTS, decision, issue_evidence,
                                     review_depth, validate_issue)
from src.report_agent.tools import BundleTools, ToolLog

logger = get_logger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
STEPS = ("carry_forward", "triage", "cluster", "deep_read", "challenge", "write")

# Periods whose items are offered to the assessor at all. History is reachable
# through search and through a carry-forward match, but it is not swept: a
# weekly report that re-triages the whole archive every week is paying to
# rediscover last month.
SHORTLIST_PERIODS = frozenset({"week", "future", "undated"})


# ── prompts ───────────────────────────────────────────────────────────────

def _client_block(profile: ClientProfile, bundle: Bundle) -> str:
    def brands(role: str) -> str:
        return ", ".join(profile.brands_with_role(role)) or "none configured"

    def bullets(lines: Sequence[str]) -> str:
        return "\n".join(f"- {line}" for line in lines) or "- none listed"

    prompt = profile.prompt
    if prompt is None:
        raise AssessorError(f"profile {profile.slug} has no 'prompt' section")
    text = (PROMPT_DIR / "_client.md").read_text(encoding="utf-8")
    values = {
        "name": profile.name, "about": prompt.about,
        "own_brands": brands("own"), "customer_brands": brands("customer"),
        "competitor_brands": brands("competitor"),
        "relevant": bullets(prompt.relevant),
        "regulatory_relevant": bullets(prompt.regulatory_relevant),
        "window": bundle.manifest["display_window"],
        "cutoff": bundle.display_cutoff,
    }
    return _fill(text, values)


def _fill(text: str, values: dict[str, str]) -> str:
    # Plain replacement rather than str.format, so a brace in the template is
    # text rather than a KeyError at six in the morning - as in src/title_gate.py.
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    unknown = sorted(set(re.findall(r"\{(\w+)\}", text)))
    if unknown:
        # A mistyped placeholder would otherwise reach the model as literal text
        # and quietly cost the step the context it was meant to have.
        raise AssessorError(f"prompt has unknown placeholders: {unknown}")
    return text.strip()


def render_prompt(step: str, client_block: str) -> str:
    return _fill((PROMPT_DIR / f"{step}.md").read_text(encoding="utf-8"),
                 {"client": client_block})


def prompt_version(step: str, system: str) -> str:
    return f"{step}-" + hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]


# ── reply schemas ─────────────────────────────────────────────────────────

def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
STRS = _array(STR)
INTS = _array(INT)

CARRY_SCHEMA = _obj({"issues": _array(_obj({
    "issue_id": STR, "matched": INTS, "searched": STRS,
    "status_now": STR, "still_open": BOOL}))})

TRIAGE_SCHEMA = _obj({"items": _array(_obj({
    "n": INT, "keep": BOOL, "reason": STR,
    "bucket": {"type": "string", "enum": ["regulatory", "customer_platform",
                                          "competitor_market", "transport_disruption",
                                          "product_safety", "own_brand", "other"]}}))})

CLUSTER_SCHEMA = _obj({
    "stories": _array(_obj({
        "story_id": STR, "label": STR, "item_ids": INTS, "primary_id": INT,
        "carries_issue_id": STR,
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "merge_note": STR})),
    "unclustered": _array(_obj({"raw_item_id": INT, "reason": STR}))})

DEEP_SCHEMA = _obj({
    "story_id": STR,
    "status": {"type": "string",
               "enum": ["reportable", "background_only", "insufficient_evidence"]},
    "headline": STR, "what_happened": STR, "why_it_matters": STR,
    "recommended_check": STR, "scope_limits": STRS,
    "excerpts": _array(_obj({"raw_item_id": INT, "quote": STR})),
    "cited_ids": INTS, "next_trigger": STR,
    "use": {"type": "string", "enum": ["main", "conditional_watch", "background"]}})

CHALLENGE_SCHEMA = _obj({
    "problems": _array(_obj({
        "severity": {"type": "string", "enum": ["error", "caution"]},
        "claim": STR, "source_says": STR, "correction": STR})),
    "corrected": _obj({
        "what_happened": STR, "why_it_matters": STR, "scope_limits": STRS,
        "status": {"type": "string",
                   "enum": ["reportable", "background_only", "insufficient_evidence"]}}),
    "verdict": STR})

WRITE_SCHEMA = _obj({
    "title": STR, "dateline": STR, "scope_note": STR, "verdict": STR,
    "sections": _array(_obj({"heading": STR, "story_ids": STRS, "markdown": STR})),
    "watchlist_markdown": STR, "coverage_markdown": STR})


# ── result ────────────────────────────────────────────────────────────────

@dataclass
class AssessResult:
    bundle: Bundle
    decisions: list[dict[str, Any]] = field(default_factory=list)
    register: list[dict[str, Any]] = field(default_factory=list)
    report_draft: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reportable(self) -> int:
        return sum(1 for issue in self.register if issue["use"] != "background")


# ── the stage ─────────────────────────────────────────────────────────────

def run_assessment(bundle: Bundle, *, model: str, config: dict[str, Any],
                   timeout: float = 300.0, max_stories: int | None = None,
                   skip_write: bool = False) -> AssessResult:
    """Run all six steps over one bundle and write stage 2's artefacts."""
    started = time.time()
    profile = load_profile(bundle.client_slug)
    if profile.profile_version != bundle.manifest["profile_version"]:
        # The bundle froze the profile that gated its items. Assessing under a
        # different one would put two versions behind one traceability key.
        logger.warning("[assess] profile is %s but the bundle froze %s",
                       profile.profile_version, bundle.manifest["profile_version"])
    client_block = _client_block(profile, bundle)
    assessor = Assessor(model=model, timeout=timeout)
    log = ToolLog()
    tools = BundleTools(bundle, log)
    result = AssessResult(bundle=bundle)
    versions: dict[str, str] = {}

    def system_for(step: str) -> str:
        system = render_prompt(step, client_block)
        versions[step] = prompt_version(step, system)
        return system

    def note(step: str, **fields: Any) -> None:
        result.steps.append({"step": step, "prompt_version": versions.get(step),
                             **fields})

    # -- step 1: carry-forward ------------------------------------------
    carried = _carry_forward(assessor, bundle, tools, system_for("carry_forward"))
    note("carry_forward", issues=len(carried["issues"]),
         matched=sum(len(i["matched"]) for i in carried["issues"]),
         skipped=carried.get("skipped"))

    pool = [row for row in bundle.evidence
            if row["source_kind"] != "dsa" and row["period"] in SHORTLIST_PERIODS]
    carried_ids = {raw_id for issue in carried["issues"] for raw_id in issue["matched"]
                   if raw_id in bundle.by_id}
    # A carried match may be an older item; it joins the shortlist regardless of
    # period, because that is the whole point of step 1.
    pool_ids = {row["id"] for row in pool}
    pool += [bundle.by_id[raw_id] for raw_id in sorted(carried_ids - pool_ids)]

    # -- step 2: triage -------------------------------------------------
    triage = _triage(assessor, pool, system_for("triage"),
                     batch_size=int(config.get("triage_batch_size", 25)),
                     effort=_effort(config, "triage", "low"))
    shortlist = [row for row in pool if triage[row["id"]]["keep"]]
    # Anything step 1 tied to an open issue survives triage: the issue is the
    # reason it matters, and its title alone would not show that.
    for raw_id in sorted(carried_ids):
        if raw_id in bundle.by_id and all(r["id"] != raw_id for r in shortlist):
            shortlist.append(bundle.by_id[raw_id])
    note("triage", offered=len(pool), kept=len(shortlist))

    # -- step 3: cluster ------------------------------------------------
    clustered = _cluster(assessor, bundle, shortlist, carried, system_for("cluster"),
                         effort=_effort(config, "cluster", "medium"))
    stories = clustered["stories"]
    if max_stories and len(stories) > max_stories:
        order = {"high": 0, "medium": 1, "low": 2}
        stories.sort(key=lambda s: order.get(s.get("priority"), 3))
        dropped = stories[max_stories:]
        stories = stories[:max_stories]
        logger.warning("[assess] %d stories over the %d cap were not read",
                       len(dropped), max_stories)
        clustered["over_cap"] = [s["story_id"] for s in dropped]
    note("cluster", stories=len(stories), unclustered=len(clustered["unclustered"]))

    # -- steps 4 and 5: read, then challenge ----------------------------
    deep_system = system_for("deep_read")
    challenge_system = system_for("challenge")
    read: list[dict[str, Any]] = []
    for story in stories:
        drafted = _deep_read(assessor, bundle, log, story, deep_system,
                             effort=_effort(config, "deep_read", "high"),
                             budget=int(config.get("deep_read_tool_calls", 30)))
        checked = _challenge(assessor, bundle, log, story, drafted, challenge_system,
                             effort=_effort(config, "challenge", "high"),
                             budget=int(config.get("challenge_tool_calls", 20)))
        result.problems.extend({"story_id": story["story_id"], **problem}
                               for problem in checked.get("problems") or [])
        read.append(drafted)
    note("deep_read", stories=len(read),
         reportable=sum(1 for s in read if s["status"] == "reportable"))
    note("challenge", problems=len(result.problems),
         errors=sum(1 for p in result.problems if p["severity"] == "error"))

    # -- the register and the bijection ---------------------------------
    result.register = _build_register(bundle, stories, read, carried)
    result.decisions = _build_decisions(bundle, log, triage, clustered, stories, read,
                                        carried_ids)

    # -- step 6: write --------------------------------------------------
    if not skip_write:
        result.report_draft = _write(assessor, bundle, result.register, system_for("write"),
                                     effort=_effort(config, "write", "high"))
        note("write", sections=len(result.report_draft.get("sections", [])))

    _persist(bundle, result, log, assessor, versions, model, started, carried, clustered)
    return result


def _effort(config: dict[str, Any], step: str, fallback: str) -> str:
    return str((config.get("steps") or {}).get(step, {}).get("reasoning_effort", fallback))


# ── steps ─────────────────────────────────────────────────────────────────

def _carry_forward(assessor: Assessor, bundle: Bundle, tools: BundleTools,
                   system: str) -> dict[str, Any]:
    step_tools = tools.for_story("carry_forward", None)
    previous = step_tools.open_issues()
    open_issues = [i for i in previous["issues"] if i.get("use") != "closed"]
    if not open_issues:
        logger.info("[assess] no open issues carried in; first cycle for this client")
        return {"issues": [], "skipped": "no earlier register", "previous": previous}
    user = ("Open issues from " + str(previous["source"]) + ":\n\n"
            + json.dumps([{k: v for k, v in issue.items()
                           if k in ("id", "label", "use", "status", "next", "current")}
                          for issue in open_issues], ensure_ascii=False, indent=1))
    reply = assessor.agent("carry_forward", system, user, CARRY_SCHEMA, step_tools,
                           effort="medium", max_tool_calls=40)
    known = {i["id"] for i in open_issues}
    issues = [i for i in reply.get("issues", []) if i.get("issue_id") in known]
    for issue in issues:
        issue["matched"] = [raw_id for raw_id in issue.get("matched", [])
                            if raw_id in bundle.by_id]
    return {"issues": issues, "previous": previous,
            "open": {i["id"]: i for i in open_issues}}


def _triage(assessor: Assessor, pool: list[dict[str, Any]], system: str, *,
            batch_size: int, effort: str) -> dict[int, dict[str, Any]]:
    verdicts: dict[int, dict[str, Any]] = {}
    for start in range(0, len(pool), batch_size):
        batch = pool[start:start + batch_size]
        user = "\n".join(_triage_line(n, row) for n, row in enumerate(batch, 1))
        reply = assessor.structured("triage", system, user, TRIAGE_SCHEMA, effort=effort)
        seen = {item["n"]: item for item in reply.get("items", [])}
        for n, row in enumerate(batch, 1):
            item = seen.get(n)
            if item is None:
                # A missing verdict keeps the item: the shortlist is recoverable
                # downstream, a silent drop is not.
                verdicts[row["id"]] = {"keep": True, "reason": "no triage verdict returned",
                                       "bucket": "other"}
            else:
                verdicts[row["id"]] = {"keep": bool(item["keep"]),
                                       "reason": item["reason"], "bucket": item["bucket"]}
    return verdicts


def _triage_line(n: int, row: dict[str, Any]) -> str:
    gate = (row.get("gate") or {}).get("payload") or {}
    day = row["event_or_publication_day"] or "undated"
    reason = gate.get("reason") or ("title gate routed this for a body fetch"
                                    if row["route"] == "title_gate_body" else "")
    extra = ""
    if row["source_kind"] == "safety_gate":
        extra = " | client match: " + ", ".join(row.get("client_reasons") or [])
    return (f"{n}. [{row['id']}] {day} ({row['date_provenance']}) "
            f"{row['source_slug']} — {row['title']}"
            + (f" | gate: {reason}" if reason else "") + extra)


def _cluster(assessor: Assessor, bundle: Bundle, shortlist: list[dict[str, Any]],
             carried: dict[str, Any], system: str, *, effort: str) -> dict[str, Any]:
    rows = [{"raw_item_id": row["id"], "day": row["event_or_publication_day"],
             "source": row["source_slug"], "kind": row["source_kind"],
             "title": row["title"],
             "opening": ((row.get("payload") or {}).get("body_text") or "")[:300]}
            for row in shortlist]
    open_issues = [{"id": issue["issue_id"], "status": issue["status_now"],
                    "matched": issue["matched"]}
                   for issue in carried["issues"] if issue.get("still_open", True)]
    user = ("Shortlist:\n" + json.dumps(rows, ensure_ascii=False, indent=1)
            + "\n\nOpen issues carried forward:\n"
            + json.dumps(open_issues, ensure_ascii=False, indent=1))
    reply = assessor.structured("cluster", system, user, CLUSTER_SCHEMA, effort=effort)

    known = {row["id"] for row in shortlist}
    stories = []
    placed: set[int] = set()
    for story in reply.get("stories", []):
        ids = [raw_id for raw_id in story.get("item_ids", []) if raw_id in known]
        if not ids:
            continue
        story["item_ids"] = ids
        if story.get("primary_id") not in ids:
            story["primary_id"] = ids[0]
        stories.append(story)
        placed.update(ids)
    unclustered = [{"raw_item_id": row["raw_item_id"], "reason": row.get("reason", "")}
                   for row in reply.get("unclustered", [])
                   if row.get("raw_item_id") in known and row["raw_item_id"] not in placed]
    # Anything the model forgot is unclustered rather than lost: the bijection
    # in stage 4 would fail otherwise, which is the check working.
    forgotten = known - placed - {row["raw_item_id"] for row in unclustered}
    unclustered += [{"raw_item_id": raw_id, "reason": "not placed by the cluster step"}
                    for raw_id in sorted(forgotten)]
    return {"stories": stories, "unclustered": unclustered}


def _deep_read(assessor: Assessor, bundle: Bundle, log: ToolLog, story: dict[str, Any],
               system: str, *, effort: str, budget: int) -> dict[str, Any]:
    story_id = story["story_id"]
    tools = BundleTools(bundle, log, step="deep_read", story_id=story_id)
    rows = [_story_row(bundle, raw_id) for raw_id in story["item_ids"]]
    user = (f"Story `{story_id}` — {story.get('label', '')}\n"
            f"Primary item: {story.get('primary_id')}\n"
            f"Continues open issue: {story.get('carries_issue_id') or 'none'}\n"
            f"Cluster note: {story.get('merge_note', '')}\n\n"
            "Items in this story:\n" + json.dumps(rows, ensure_ascii=False, indent=1))
    logger.info("[assess] deep read %s (%d items)", story_id, len(rows))
    drafted = assessor.agent("deep_read", system, user, DEEP_SCHEMA, tools,
                             effort=effort, max_tool_calls=budget)
    drafted["story_id"] = story_id
    drafted["cited_ids"] = [raw_id for raw_id in drafted.get("cited_ids", [])
                            if raw_id in bundle.by_id] or list(story["item_ids"])
    drafted["excerpts"] = [e for e in drafted.get("excerpts", [])
                           if e.get("raw_item_id") in bundle.by_id]
    return drafted


def _challenge(assessor: Assessor, bundle: Bundle, log: ToolLog, story: dict[str, Any],
               drafted: dict[str, Any], system: str, *, effort: str,
               budget: int) -> dict[str, Any]:
    tools = BundleTools(bundle, log, step="challenge", story_id=story["story_id"])
    user = ("Draft story:\n" + json.dumps(drafted, ensure_ascii=False, indent=1)
            + "\n\nThe records behind it:\n"
            + json.dumps([_story_row(bundle, raw_id) for raw_id in drafted["cited_ids"]],
                         ensure_ascii=False, indent=1))
    checked = assessor.agent("challenge", system, user, CHALLENGE_SCHEMA, tools,
                             effort=effort, max_tool_calls=budget)
    corrected = checked.get("corrected") or {}
    problems = checked.get("problems") or []
    if problems:
        logger.info("[assess] challenge %s: %d problem(s)", story["story_id"], len(problems))
    # The corrected text is what the report uses. A challenge that found nothing
    # returns the draft unchanged, so this is a no-op in that case.
    for field_name in ("what_happened", "why_it_matters", "status"):
        if corrected.get(field_name):
            drafted[field_name] = corrected[field_name]
    # The challenge returns the limits as they should stand *after* its
    # corrections, so they replace the draft's rather than being appended to
    # them - appending restated half of them in the customer's own report.
    if corrected.get("scope_limits"):
        drafted["scope_limits"] = list(corrected["scope_limits"])
    drafted["challenge"] = {"verdict": checked.get("verdict", ""), "problems": problems}
    return checked


def _write(assessor: Assessor, bundle: Bundle, register: list[dict[str, Any]],
           system: str, *, effort: str) -> dict[str, Any]:
    reportable = [issue for issue in register if issue["use"] != "background"]
    coverage = bundle.maybe("coverage.json", [])
    zero_yield = sorted({row["source"] for row in coverage
                         if row.get("latest_status") == "zero"})
    unavailable = bundle.unavailable
    user = ("Issue register:\n"
            + json.dumps(reportable, ensure_ascii=False, indent=1)
            + "\n\nCoverage facts you may state:\n"
            + json.dumps({
                "sources_run": len(coverage),
                "zero_yield_latest_run": zero_yield,
                "titles_without_a_usable_body": [
                    {"raw_item_id": row["id"], "source": row["source_slug"],
                     "title": row["title"], "body_status": row.get("body_status")}
                    for row in unavailable],
                "window": bundle.manifest["display_window"],
                "cutoff": bundle.display_cutoff,
                "candidates_assessed": bundle.manifest["evidence_items"],
                "identities_accounted_for": bundle.manifest["ledger_identities"],
                "sources_that_found_nothing_in_their_latest_run": len(zero_yield),
                "policy": bundle.manifest["policy"]}, ensure_ascii=False, indent=1))
    return assessor.structured("write", system, user, WRITE_SCHEMA, effort=effort)


# ── assembling the output ─────────────────────────────────────────────────

def _story_row(bundle: Bundle, raw_id: int) -> dict[str, Any]:
    row = bundle.by_id[raw_id]
    gate = (row.get("gate") or {}).get("payload") or {}
    return {"raw_item_id": row["id"], "source": row["source_slug"],
            "kind": row["source_kind"], "title": row["title"], "url": row["url"],
            "day": row["event_or_publication_day"],
            "date_provenance": row["date_provenance"], "period": row["period"],
            "body_status": (row.get("payload") or {}).get("body_status"),
            "gate_reason": gate.get("reason")}


def _build_register(bundle: Bundle, stories: list[dict[str, Any]],
                    read: list[dict[str, Any]],
                    carried: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per issue: dated status, the evidence, and the next trigger."""
    previous = {issue["id"]: issue for issue in (carried.get("previous") or {}).get("issues", [])}
    by_story = {story["story_id"]: story for story in stories}
    cutoff = bundle.manifest["window_end_exclusive"]
    register: list[dict[str, Any]] = []
    covered_issues: set[str] = set()

    for drafted in read:
        story = by_story[drafted["story_id"]]
        issue_id = story.get("carries_issue_id") or story["story_id"]
        covered_issues.add(issue_id)
        earlier = previous.get(issue_id, {})
        use = drafted.get("use", "main")
        if drafted["status"] == "background_only":
            use = "background"
        register.append({
            "id": issue_id, "label": story.get("label") or drafted.get("headline", ""),
            "use": use,
            "status": drafted.get("headline", ""),
            "what_happened": drafted.get("what_happened", ""),
            "why_it_matters": drafted.get("why_it_matters", ""),
            "recommended_check": drafted.get("recommended_check", ""),
            "scope_limits": drafted.get("scope_limits", []),
            "next": drafted.get("next_trigger", ""),
            "note": story.get("merge_note", ""),
            "assessment_status": drafted["status"],
            "baseline": list(earlier.get("current") or earlier.get("baseline") or []),
            "current": list(story["item_ids"]),
            "primary": story.get("primary_id"),
            "excerpts": drafted.get("excerpts", []),
            "challenge": drafted.get("challenge", {}),
            "client": bundle.client_slug, "report_cutoff": cutoff,
            "evidence": [issue_evidence(bundle.by_id[raw_id])
                         for raw_id in story["item_ids"] if raw_id in bundle.by_id],
        })

    # Open issues with no story this week stay in the register, dated and
    # explicitly not updated. The next cycle starts from this, not from prose.
    for issue in carried["issues"]:
        if issue["issue_id"] in covered_issues:
            continue
        earlier = previous.get(issue["issue_id"], {})
        register.append({
            "id": issue["issue_id"], "label": earlier.get("label", issue["issue_id"]),
            "use": "conditional_watch" if issue.get("still_open", True) else "closed",
            "status": issue["status_now"],
            "what_happened": "", "why_it_matters": "",
            "recommended_check": "",
            "scope_limits": [f"searched this window for: {'; '.join(issue.get('searched', []))}"]
                            if issue.get("searched") else [],
            "next": earlier.get("next", ""), "note": "no qualifying development this window",
            "assessment_status": "carry_forward",
            "baseline": list(earlier.get("current") or earlier.get("baseline") or []),
            "current": list(issue.get("matched") or []),
            "primary": None, "excerpts": [], "challenge": {},
            "client": bundle.client_slug, "report_cutoff": cutoff,
            "evidence": [issue_evidence(bundle.by_id[raw_id])
                         for raw_id in issue.get("matched", []) if raw_id in bundle.by_id],
        })

    known = set(bundle.by_id)
    complaints = [problem for issue in register for problem in validate_issue(issue, known)]
    for complaint in complaints:
        logger.warning("[assess] register: %s", complaint)
    return register


def _build_decisions(bundle: Bundle, log: ToolLog, triage: dict[int, dict[str, Any]],
                     clustered: dict[str, Any], stories: list[dict[str, Any]],
                     read: list[dict[str, Any]], carried_ids: set[int]
                     ) -> list[dict[str, Any]]:
    """Exactly one decision per identity in the export. No gaps, no duplicates."""
    drafted = {item["story_id"]: item for item in read}
    story_of: dict[int, list[str]] = {}
    treatment_of: dict[int, tuple[str, str, str]] = {}

    for story in stories:
        item = drafted.get(story["story_id"])
        if item is None:  # over the story cap; never read
            for raw_id in story["item_ids"]:
                treatment_of[raw_id] = ("omit_for_priority",
                                        "over this run's story cap; not read", "cluster")
                story_of.setdefault(raw_id, []).append(story["story_id"])
            continue
        cited = set(item["cited_ids"]) | {story.get("primary_id")}
        for raw_id in story["item_ids"]:
            story_of.setdefault(raw_id, []).append(story["story_id"])
            if item["status"] == "insufficient_evidence":
                treatment_of[raw_id] = ("insufficient_evidence",
                                        item.get("headline", ""), "deep_read")
            elif item["status"] == "background_only":
                treatment_of[raw_id] = ("background_only", item.get("headline", ""),
                                        "deep_read")
            elif raw_id in cited:
                treatment_of[raw_id] = ("report", item.get("headline", ""), "deep_read")
            else:
                treatment_of[raw_id] = ("merge",
                                        story.get("merge_note") or "same development as "
                                        f"{story.get('primary_id')}", "cluster")

    for row in clustered["unclustered"]:
        treatment_of.setdefault(row["raw_item_id"],
                                ("omit_for_priority", row["reason"] or
                                 "shortlisted but not part of a reported story", "cluster"))

    decisions: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(raw_id: int, treatment: str, reason: str, step: str, by: str,
            row: dict[str, Any]) -> None:
        if raw_id in seen:
            return
        seen.add(raw_id)
        decisions.append(decision(raw_id, treatment, reason, decided_by=by, step=step,
                                  story_ids=story_of.get(raw_id, ()),
                                  review_depth_value=review_depth(row, log.tools_for(raw_id))))

    for row in bundle.evidence:
        raw_id = row["id"]
        if raw_id in treatment_of:
            treatment, reason, step = treatment_of[raw_id]
            add(raw_id, treatment, reason, step, "assessor", row)
        elif raw_id in triage and not triage[raw_id]["keep"]:
            add(raw_id, "omit", triage[raw_id]["reason"], "triage", "assessor", row)
        elif raw_id in triage:
            # Kept by triage, then neither clustered nor omitted - only reachable
            # if a later step lost it. Recorded as such rather than hidden.
            add(raw_id, "omit_for_priority", "kept at triage but not placed in a story",
                "cluster", "assessor", row)
        else:
            treatment, reason = _rule_treatment(row, raw_id in carried_ids)
            add(raw_id, treatment, reason, "rule", "rule", row)

    for row in bundle.stopped:
        gate = (row.get("gate") or {}).get("payload") or {}
        add(row["id"], "retain_gate_stop",
            gate.get("reason") or "stopped by the gate", "rule", "rule", row)

    for row in bundle.unavailable:
        add(row["id"], "unavailable_body",
            f"routed for a body fetch; body_status={row.get('body_status')}, "
            f"fetch={row.get('fetch_status')}", "rule", "rule", row)

    return decisions


def _rule_treatment(row: dict[str, Any], carried: bool) -> tuple[str, str]:
    if row["source_kind"] == "dsa":
        return ("background_not_reported",
                RULE_TREATMENTS["background_not_reported"])
    if row["source_kind"] == "safety_gate":
        return ("historical_pattern_reference",
                "pre-window Safety Gate record in the client view")
    if row["period"] == "undated":
        return ("undated_background", RULE_TREATMENTS["undated_background"])
    if carried:
        return ("background_only", "older evidence supporting an open issue")
    return ("archive_no_week_update", RULE_TREATMENTS["archive_no_week_update"])


# ── persistence ───────────────────────────────────────────────────────────

def _persist(bundle: Bundle, result: AssessResult, log: ToolLog, assessor: Assessor,
             versions: dict[str, str], model: str, started: float,
             carried: dict[str, Any], clustered: dict[str, Any]) -> None:
    bundle.write("issue-register.json", result.register)
    bundle.write("decisions.json", result.decisions)
    if result.report_draft:
        bundle.write("report-draft.json", result.report_draft)
    bundle.write("cluster.json", clustered)
    bundle.write("carry-forward.json", {"issues": carried["issues"],
                                        "previous_bundle": (carried.get("previous") or {}).get("source"),
                                        "skipped": carried.get("skipped")})
    bundle.write_text("tool-calls.jsonl", "\n".join(
        json.dumps(call, ensure_ascii=False) for call in log.as_jsonl_rows()) + "\n")
    bundle.write("assessment-manifest.json", {
        "assessed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seconds": round(time.time() - started, 1),
        "model": model, "prompt_versions": versions,
        "profile_version": bundle.manifest["profile_version"],
        "steps": result.steps, "usage": assessor.totals(),
        "tool_calls": len(log.calls),
        "identities_decided": len(result.decisions),
        "identities_in_export": bundle.manifest["ledger_identities"],
        "challenge_problems": result.problems,
        "cost_note": "Token counts are measured; a per-cycle cost is not - "
                     "report_plan.md §8 leaves it open until a price is pinned.",
    })


def load_config() -> dict[str, Any]:
    """The report_agent section of config.json, per-step efforts included."""
    from src.config import REPORT_AGENT

    if not REPORT_AGENT:
        raise AssessorError("config.json has no 'report_agent' section")
    return REPORT_AGENT
