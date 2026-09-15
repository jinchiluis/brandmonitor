"""The vocabulary stages 2, 3 and 4 share: treatments, depth, and the register.

Relevance is not treatment. The gates already answered "is this a signal for
this client"; the report needs a second and different answer - what happens to
it this week. That answer is the ``treatment``, and it is the only thing the
ledger, the renderer and the verifier all agree on, so it lives here rather
than in any one of them.

Two families, kept apart on purpose (report_plan.md §7). ``RULE_TREATMENTS``
are written by code from the frozen export - there is no reason to pay a model
to say ``archive_no_week_update`` two hundred times. ``ASSESSOR_TREATMENTS``
are the ones that need judgment, and a decision carrying one must name the step
that produced it.
"""

from __future__ import annotations

from typing import Any, Iterable

# -- what happened to an identity this week --------------------------------

ASSESSOR_TREATMENTS = {
    "report": "carries its own finding in the report",
    "merge": "folded into another item's story; names the primary",
    "background_only": "used as context inside another finding, not reported alone",
    "carry_forward": "an open issue with no qualifying development this week",
    "insufficient_evidence": "would be reportable, but the stored material "
                             "cannot support the claim",
    "omit_for_priority": "real and client-adjacent, but not worth the week",
    "omit": "not a signal for this client on a second look",
}

RULE_TREATMENTS = {
    "retain_gate_stop": "a gate stopped it; retained for audit and carry-forward search",
    "archive_no_week_update": "eligible older material with no bearing on an open issue",
    "background_not_reported": "DSA submission aggregate; no event-date basis to report",
    "historical_pattern_reference": "pre-window Safety Gate record kept as pattern context",
    "undated_background": "no trustworthy publication date; not datable as a week event",
    "unavailable_body": "routed for a body that never arrived; title evidence only",
}

TREATMENTS = {**ASSESSOR_TREATMENTS, **RULE_TREATMENTS}

# Treatments that put an item in front of the customer in some form. The verifier
# warns when the report links an identity outside its coverage notes whose
# treatment is not one of these; the renderer itself cites any frozen identity.
REPORTED = frozenset({"report", "merge", "background_only", "carry_forward"})

# -- how deeply an identity was actually read ------------------------------
#
# Derived from the assessor's logged tool calls, never asserted. The experiment
# inferred depth from hardcoded ID sets, which made the ledger's most important
# column a claim; here each value is the name of a call that happened.

# Strongest first: an item read fully and also seen in a search is read fully.
DEPTH_ORDER = ("full_stored_body", "structured_record", "title_or_overview",
               "search_snippet", "title_only")

# get_item hands back the whole record for a structured source and only the
# header for an article, so the same call means different depths.
_RECORD_KINDS = frozenset({"safety_gate", "dsa", "regulatory"})


def review_depth(row: dict[str, Any], tools_used: Iterable[str]) -> str:
    """The deepest read this identity actually received."""
    used = set(tools_used)
    if "get_body" in used:
        return "full_stored_body"
    if "get_item" in used:
        return ("structured_record" if row.get("source_kind") in _RECORD_KINDS
                else "title_or_overview")
    if used:
        return "search_snippet"
    return "title_only"


# -- decisions -------------------------------------------------------------

def decision(raw_item_id: int, treatment: str, reason: str, *, decided_by: str,
             step: str, story_ids: Iterable[str] = (),
             review_depth_value: str = "title_only") -> dict[str, Any]:
    """One row of the bijection. Every identity in the export gets exactly one."""
    if treatment not in TREATMENTS:
        raise ValueError(f"unknown treatment {treatment!r}; expected one of "
                         f"{sorted(TREATMENTS)}")
    if decided_by == "rule" and treatment not in RULE_TREATMENTS:
        raise ValueError(f"{treatment!r} needs judgment; a rule cannot write it")
    return {"raw_item_id": int(raw_item_id), "treatment": treatment,
            "reason": reason.strip(), "decided_by": decided_by, "step": step,
            "story_ids": list(story_ids), "review_depth": review_depth_value}


# -- the issue register ----------------------------------------------------
#
# The unit of report state is an issue, not an article. The next cycle starts
# from dated status plus the evidence that would trigger an update, not from
# last week's prose.

REGISTER_USES = ("main", "conditional_watch", "background", "closed")

REQUIRED_ISSUE_FIELDS = ("id", "label", "use", "status", "next")


def issue_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """The citable facts about one identity, copied out of the frozen export."""
    return {"raw_item_id": row["id"], "version": row["version"], "url": row["url"],
            "title": row["title"],
            "published_or_event_day": row["event_or_publication_day"],
            "date_provenance": row["date_provenance"],
            "source": row["source_slug"], "fetched_at": row["fetched_at"]}


def validate_issue(issue: dict[str, Any], known_ids: set[int]) -> list[str]:
    """Structural complaints about one register entry. Empty means it is sound."""
    problems = []
    for field in REQUIRED_ISSUE_FIELDS:
        if not str(issue.get(field) or "").strip():
            problems.append(f"{issue.get('id', '?')}: missing {field}")
    if issue.get("use") not in REGISTER_USES:
        problems.append(f"{issue.get('id', '?')}: use must be one of {REGISTER_USES}")
    for key in ("baseline", "current"):
        for raw_id in issue.get(key) or []:
            if raw_id not in known_ids:
                problems.append(f"{issue['id']}: {key} id {raw_id} is not in the export")
    return problems
