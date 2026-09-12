"""The assessor's read-only tool surface over one frozen bundle.

Five tools, no network, no database, no writes. The evidence is already on a
filesystem, so the assessor navigates it instead of carrying it in context -
which is what lets a step be re-run without a preserved conversation.

Keeping the surface at five mirrors the discipline that keeps the vendored
crawler's surface small. Every resolution goes through the bundle's id map, so
an id the model invents fails here rather than reaching the report.

The log is not a debugging aid. ``review_depth`` in the ledger is derived from
it: ``get_body(24617)`` was called, so that row was read fully; only a search
matched it, so it was read as a snippet. That makes the ledger's most important
column measured rather than asserted (report_plan.md §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.report_agent.bundle import Bundle, previous_bundle

# A stored body can be tens of thousands of characters; a DIP document more.
# The cap is per call, and the tool says when it truncated so the assessor can
# narrow its question rather than assume it saw the end.
BODY_CHARS = 20000
OVERVIEW_CHARS = 400
SNIPPET_CHARS = 220
DEFAULT_LIMIT = 25
MAX_LIMIT = 80

_WORD = re.compile(r"[\wÀ-ɏ]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _standing(row: dict[str, Any]) -> str:
    """candidate, stopped, or unreadable - frozen on the row by the export."""
    return row.get("in_export_as", "candidate")


@dataclass
class ToolLog:
    """What the assessor actually read, per identity and in order."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    touched: dict[int, set[str]] = field(default_factory=dict)

    def record(self, tool: str, args: dict[str, Any], ids: list[int],
               step: str, story_id: str | None = None) -> None:
        self.calls.append({"step": step, "story_id": story_id, "tool": tool,
                           "args": args, "returned": ids})
        for raw_id in ids:
            self.touched.setdefault(raw_id, set()).add(tool)

    def tools_for(self, raw_id: int) -> set[str]:
        return self.touched.get(raw_id, set())

    def as_jsonl_rows(self) -> list[dict[str, Any]]:
        return list(self.calls)


class ToolError(ValueError):
    """A tool was called with something the bundle does not contain."""


class BundleTools:
    """Bound to one bundle and one step; every call is logged."""

    def __init__(self, bundle: Bundle, log: ToolLog, *, step: str = "assess",
                 story_id: str | None = None) -> None:
        self.bundle = bundle
        self.log = log
        self.step = step
        self.story_id = story_id

    def for_story(self, step: str, story_id: str | None) -> "BundleTools":
        return BundleTools(self.bundle, self.log, step=step, story_id=story_id)

    # -- the five tools -------------------------------------------------

    def get_item(self, raw_item_id: int) -> dict[str, Any]:
        """The frozen record: identity, dates, provenance, gate verdict, overview."""
        row = self._row(raw_item_id)
        payload = row.get("payload") or {}
        gate = (row.get("gate") or {}).get("payload") or {}
        body = payload.get("body_text") or ""
        item = {
            "raw_item_id": row["id"], "version": row["version"],
            "source": row["source_slug"], "source_kind": row["source_kind"],
            "title": row["title"], "url": row["url"],
            "event_or_publication_day": row["event_or_publication_day"],
            "date_provenance": row["date_provenance"], "period": row["period"],
            "fetched_at": row["fetched_at"], "route": row["route"],
            "in_export_as": _standing(row),
            "gate_verdict": gate.get("verdict"), "gate_reason": gate.get("reason"),
            "selector_reasons": gate.get("selector_reasons") or [],
            "body_status": payload.get("body_status"),
            "body_chars": len(body),
            "body_opening": body[:OVERVIEW_CHARS],
        }
        if row["source_kind"] in ("safety_gate", "dsa"):
            # A structured record is small and is the evidence; hand it over whole
            # rather than make the assessor fetch a "body" that is a rendering.
            item["record"] = {k: v for k, v in payload.items() if k != "body_text"}
            item["client_reasons"] = row.get("client_reasons") or []
        self.log.record("get_item", {"raw_item_id": raw_item_id}, [row["id"]],
                        self.step, self.story_id)
        return item

    def get_body(self, raw_item_id: int) -> dict[str, Any]:
        """The stored body text, as extracted at collection time."""
        row = self._row(raw_item_id)
        payload = row.get("payload") or {}
        body = payload.get("body_text") or ""
        self.log.record("get_body", {"raw_item_id": raw_item_id}, [row["id"]],
                        self.step, self.story_id)
        if not body:
            return {"raw_item_id": row["id"], "body_status": payload.get("body_status"),
                    "text": "", "note": "no body stored for this identity; "
                                         "title and metadata are all the evidence there is"}
        return {"raw_item_id": row["id"], "title": row["title"], "url": row["url"],
                "body_status": payload.get("body_status"),
                "body_extractor": payload.get("body_extractor"),
                "chars": len(body), "truncated": len(body) > BODY_CHARS,
                "text": body[:BODY_CHARS]}

    def search_titles(self, query: str, limit: int = DEFAULT_LIMIT,
                      include_stopped: bool = True) -> dict[str, Any]:
        """Candidates and stopped items whose title, URL or source matches.

        Stopped items are searchable on purpose: an open issue must be able to
        find its continuation among the material a gate rejected.
        """
        rows = self.bundle.evidence + (self.bundle.stopped if include_stopped else [])
        hits = self._rank(query, rows, lambda r: f"{r['title'] or ''} {r['url'] or ''} "
                                                 f"{r['source_slug']}")
        return self._results("search_titles", {"query": query, "limit": limit,
                                               "include_stopped": include_stopped},
                             hits, limit, self._title_hit)

    def search_bodies(self, query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Stored bodies whose text matches, with the passage around the match."""
        rows = [r for r in self.bundle.evidence + self.bundle.stopped
                if (r.get("payload") or {}).get("body_text")]
        hits = self._rank(query, rows, lambda r: r["payload"]["body_text"])
        return self._results("search_bodies", {"query": query, "limit": limit},
                             hits, limit, lambda r, q: self._body_hit(r, q))

    def open_issues(self) -> dict[str, Any]:
        """Last cycle's register for this client, or an empty first cycle."""
        register, source = self._previous_register()
        self.log.record("open_issues", {}, [], self.step, self.story_id)
        return {"source": source, "issues": register,
                "note": ("no earlier bundle for this client; this is a first cycle "
                         "and every issue is new" if source is None else
                         "baseline/current ids refer to that cycle's export, not this one")}

    # -- internals ------------------------------------------------------

    def _previous_register(self) -> tuple[list[dict[str, Any]], str | None]:
        earlier = previous_bundle(self.bundle.client_slug,
                                  self.bundle.manifest["window_start"])
        if earlier is None:
            return [], None
        return earlier.maybe("issue-register.json", []), earlier.path.name

    def _row(self, raw_item_id: int) -> dict[str, Any]:
        try:
            raw_id = int(raw_item_id)
        except (TypeError, ValueError):
            raise ToolError(f"raw_item_id must be an integer, got {raw_item_id!r}")
        row = self.bundle.by_id.get(raw_id)
        if row is None:
            raise ToolError(f"raw_item_id {raw_id} is not in this frozen export. "
                            "Only ids returned by a search or listed in the "
                            "shortlist exist here.")
        return row

    @staticmethod
    def _rank(query: str, rows: list[dict[str, Any]],
              text_of) -> list[tuple[int, dict[str, Any]]]:
        terms = _tokens(query)
        if not terms:
            raise ToolError("search needs a non-empty query")
        scored = []
        for row in rows:
            haystack = (text_of(row) or "").lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0],
                                      pair[1]["event_or_publication_day"] or "",
                                      pair[1]["id"]))
        return scored

    def _results(self, tool: str, args: dict[str, Any],
                 hits: list[tuple[int, dict[str, Any]]], limit: int, render) -> dict:
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        shown = hits[:limit]
        ids = [row["id"] for _, row in shown]
        self.log.record(tool, args, ids, self.step, self.story_id)
        return {"matched": len(hits), "shown": len(shown),
                "truncated": len(hits) > len(shown),
                "results": [render(row, args["query"]) for _, row in shown]}

    def _title_hit(self, row: dict[str, Any], _query: str) -> dict[str, Any]:
        gate = (row.get("gate") or {}).get("payload") or {}
        return {"raw_item_id": row["id"], "source": row["source_slug"],
                "title": row["title"], "url": row["url"],
                "day": row["event_or_publication_day"],
                "provenance": row["date_provenance"], "period": row["period"],
                "in_export_as": _standing(row),
                "gate_verdict": gate.get("verdict"), "gate_reason": gate.get("reason")}

    def _body_hit(self, row: dict[str, Any], query: str) -> dict[str, Any]:
        text = row["payload"]["body_text"]
        lowered = text.lower()
        position = min((lowered.find(term) for term in _tokens(query)
                        if lowered.find(term) >= 0), default=0)
        start = max(0, position - SNIPPET_CHARS // 2)
        hit = self._title_hit(row, query)
        hit["snippet"] = text[start:start + SNIPPET_CHARS].replace("\n", " ")
        hit["body_chars"] = len(text)
        return hit


# -- the wire format the model sees ----------------------------------------

def tool_specs() -> list[dict[str, Any]]:
    """Responses-API function definitions for the five tools."""
    def spec(name: str, description: str, properties: dict[str, Any],
             required: list[str]) -> dict[str, Any]:
        return {"type": "function", "name": name, "description": description,
                "strict": True,
                "parameters": {"type": "object", "properties": properties,
                               "required": required, "additionalProperties": False}}

    item_id = {"raw_item_id": {"type": "integer",
                               "description": "an id from the shortlist or a search result"}}
    return [
        spec("get_item", "The frozen record for one identity: source, title, URL, "
                         "event or publication day and where that date came from, the "
                         "gate verdict and reason, and the opening of any stored body. "
                         "Structured sources return their whole record.",
             item_id, ["raw_item_id"]),
        spec("get_body", "The full stored body text for one identity. Use it before "
                         "making any claim about what an article says.",
             item_id, ["raw_item_id"]),
        spec("search_titles", "Search titles, URLs and source names across this "
                              "window's candidates AND the items a gate stopped. "
                              "Use it to find the earlier instalment of a running story.",
             {"query": {"type": "string", "description": "space-separated terms; "
                                                         "German terms work best"},
              "limit": {"type": "integer", "description": "1-80, default 25"},
              "include_stopped": {"type": "boolean",
                                  "description": "search gate-rejected items too"}},
             ["query", "limit", "include_stopped"]),
        spec("search_bodies", "Search the stored body text and return the passage "
                              "around each match.",
             {"query": {"type": "string"}, "limit": {"type": "integer"}},
             ["query", "limit"]),
        spec("open_issues", "Last cycle's issue register for this client.",
             {}, []),
    ]


def dispatch(tools: BundleTools, name: str, args: dict[str, Any]) -> Any:
    """Route one model tool call. Unknown names and bad ids raise ToolError."""
    if name == "get_item":
        return tools.get_item(args["raw_item_id"])
    if name == "get_body":
        return tools.get_body(args["raw_item_id"])
    if name == "search_titles":
        return tools.search_titles(args["query"], args.get("limit") or DEFAULT_LIMIT,
                                   args.get("include_stopped", True))
    if name == "search_bodies":
        return tools.search_bodies(args["query"], args.get("limit") or DEFAULT_LIMIT)
    if name == "open_issues":
        return tools.open_issues()
    raise ToolError(f"no such tool: {name}")
