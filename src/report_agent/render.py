"""Stage 3: join the assessor's decisions against the frozen export and render.

Deterministic. It knows nothing about a model and makes no judgment: every
sentence in the Chinese report came from stage 2, every ledger row is either a
stage 2 decision or a rule, and every link resolves through the frozen export.

That last part is the load-bearing one. The write step emits links as
``[文字](item:24617)`` and this module substitutes the URL the export froze for
that id. An id that is not in the export fails the build here, which is what
makes a fabricated source structurally impossible rather than something a
reviewer has to notice.

Four pages, all local, no service and no network:

    weekly-report.zh   the customer deliverable
    editorial-ledger   what happened to every identity, and how deeply it was read
    source-coverage    every source that ran, including the ones that found nothing
    source-evidence    the frozen titles and bodies the ledger links into

Writing nothing to the database is deliberate: a render must be repeatable, and
``run.py report --record`` is the separate, explicit step that files the row.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.logger import get_logger
from src.report_agent.bundle import Bundle
from src.report_agent.schema import TREATMENTS

logger = get_logger(__name__)

PAGES = (("weekly-report.zh", "中文周报"), ("editorial-ledger", "Editorial ledger"),
         ("source-coverage", "Source coverage"), ("source-evidence", "Stored evidence"))

ITEM_LINK = re.compile(r"\[([^\]]+)\]\(item:(\d+)\)")
MD_LINK = re.compile(r"\[([^\]]+)\]\(((?:[^()]|\([^()]*\))*)\)")


class RenderError(RuntimeError):
    """The draft references something the frozen export does not contain."""


@dataclass
class RenderResult:
    written: list[str] = field(default_factory=list)
    cited_ids: set[int] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)


# ── the deliverable ───────────────────────────────────────────────────────

def render_bundle(bundle: Bundle) -> RenderResult:
    draft = bundle.maybe("report-draft.json")
    decisions = bundle.maybe("decisions.json")
    if draft is None or decisions is None:
        raise RenderError(f"{bundle.path.name} has no assessment yet; "
                          "run `python run.py assess --bundle` first")
    register = bundle.maybe("issue-register.json", [])
    result = RenderResult()

    report_md, cited = _report_markdown(bundle, draft)
    result.cited_ids = cited
    ledger = _ledger(bundle, decisions, register)
    bundle.write("editorial-ledger.json", ledger)

    documents = {
        "weekly-report.zh.md": report_md,
        "editorial-ledger.md": _ledger_markdown(bundle, ledger),
        "source-coverage.md": _coverage_markdown(bundle),
    }
    for name, text in documents.items():
        bundle.write_text(name, text)
        stem = name[: -len(".md")]
        lang = "zh-CN" if stem.endswith(".zh") else "en"
        bundle.write_text(f"{stem}.html",
                          _page(bundle, _title_of(text), _markdown_to_html(text), lang))
        result.written += [name, f"{stem}.html"]

    bundle.write_text("source-evidence.html", _evidence_page(bundle, ledger))
    bundle.write_text("README.md", _readme(bundle, register, ledger))
    result.written += ["source-evidence.html", "README.md", "editorial-ledger.json"]
    logger.info("[report] %s: %d ledger rows, %d cited sources",
                bundle.path.name, len(ledger), len(cited))
    return result


def _report_markdown(bundle: Bundle, draft: dict[str, Any]) -> tuple[str, set[int]]:
    cited: set[int] = set()

    def resolve(text: str) -> str:
        return _resolve_item_links(bundle, text or "", cited)

    def block(text: str) -> str:
        return resolve(_without_leading_heading(text))

    lines = [f"# {resolve(draft.get('title', '周报'))}", "",
             f"**{resolve(draft.get('dateline', bundle.manifest['display_window']))}**", ""]
    if draft.get("scope_note"):
        lines += [block(draft["scope_note"]), ""]
    if draft.get("verdict"):
        lines += ["## 本周判断", "", block(draft["verdict"]), ""]
    for n, section in enumerate(draft.get("sections", []), 1):
        lines += [f"## {n}. {resolve(section.get('heading', ''))}", "",
                  block(section.get("markdown", "")), ""]
    if draft.get("watchlist_markdown"):
        lines += ["## 延续事项与接下来要看的节点", "",
                  block(draft["watchlist_markdown"]), ""]
    if draft.get("coverage_markdown"):
        lines += ["## 覆盖与证据限制", "", block(draft["coverage_markdown"]), ""]
    return "\n".join(lines).rstrip() + "\n", cited


def _without_leading_heading(text: str) -> str:
    """Drop a section heading the writer repeated inside its own block.

    The renderer owns the headings - it numbers the sections - so a writer that
    also writes one produces a duplicate. Cheaper to drop it here than to make
    the prompt's shape rules carry the weight.
    """
    lines = (text or "").lstrip().splitlines()
    if lines and lines[0].startswith("#"):
        return "\n".join(lines[1:]).lstrip()
    return text or ""


def _resolve_item_links(bundle: Bundle, text: str, cited: set[int]) -> str:
    """Turn every ``item:NNN`` link into the URL the export froze for that id."""
    def substitute(match: re.Match[str]) -> str:
        raw_id = int(match.group(2))
        row = bundle.by_id.get(raw_id)
        if row is None:
            raise RenderError(
                f"the report links item:{raw_id}, which is not in the frozen export")
        cited.add(raw_id)
        url = row.get("url")
        if not url:
            # A structured record with no public URL - a DSA aggregate. Naming it
            # is honest; inventing a link for it is not.
            return f"{match.group(1)}（{row['source_slug']} 记录 {raw_id}）"
        return f"[{match.group(1)}]({url})"

    resolved = ITEM_LINK.sub(substitute, text)
    stray = [m.group(2) for m in MD_LINK.finditer(resolved)
             if m.group(2).startswith(("http://", "https://"))]
    for url in stray:
        if not any(row.get("url") == url for row in bundle.by_id.values()):
            raise RenderError(f"the report contains a URL that is not in the frozen "
                              f"evidence: {url}")
    return resolved


# ── the ledger ────────────────────────────────────────────────────────────

def _ledger(bundle: Bundle, decisions: list[dict[str, Any]],
            register: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per identity, joined to the frozen record. Fails on a gap."""
    rows = bundle.by_id
    issue_of: dict[str, str] = {issue["id"]: issue.get("label", issue["id"])
                                for issue in register}
    seen: set[int] = set()
    ledger = []
    for entry in decisions:
        raw_id = entry["raw_item_id"]
        if raw_id in seen:
            raise RenderError(f"identity {raw_id} has more than one decision")
        seen.add(raw_id)
        row = rows.get(raw_id)
        if row is None:
            raise RenderError(f"decision for {raw_id}, which is not in the export")
        ledger.append({
            "raw_item_id": raw_id, "version": row["version"],
            "source": row["source_slug"], "title": row["title"],
            "period": row.get("period"), "date": row.get("event_or_publication_day"),
            "provenance": row.get("date_provenance"), "route": row.get("route"),
            "treatment": entry["treatment"],
            "treatment_means": TREATMENTS[entry["treatment"]],
            "reason": entry["reason"], "decided_by": entry["decided_by"],
            "review_depth": entry["review_depth"],
            "stories": [issue_of.get(s, s) for s in entry.get("story_ids", [])],
        })
    missing = sorted(set(rows) - seen)
    if missing:
        raise RenderError(f"{len(missing)} identities have no decision "
                          f"(first: {missing[:5]}); the assessment is incomplete")
    ledger.sort(key=lambda r: r["raw_item_id"])
    return ledger


def _ledger_markdown(bundle: Bundle, ledger: list[dict[str, Any]]) -> str:
    counts = _tally(row["treatment"] for row in ledger)
    depths = _tally(row["review_depth"] for row in ledger)
    lines = ["# Editorial ledger", "",
             f"Every identity in the frozen export for {bundle.manifest['display_window']}, "
             f"with what happened to it and how deeply it was actually read. "
             f"{len(ledger)} rows, one per identity.", "",
             "Review depth is derived from the assessor's logged tool calls, not "
             "asserted: `full_stored_body` means `get_body` was called on that id, "
             "`title_only` means nothing was.", "",
             "## Treatments", "", "| Treatment | Rows | Means |", "|---|---:|---|"]
    for treatment, n in counts:
        lines.append(f"| `{treatment}` | {n} | {TREATMENTS[treatment]} |")
    lines += ["", "## Measured review depth", "", "| Depth | Rows |", "|---|---:|"]
    lines += [f"| `{depth}` | {n} |" for depth, n in depths]
    lines += ["", "## Every identity", "",
              "| ID | Date / provenance | Source | Title | Treatment | Depth | Reason |",
              "|---|---|---|---|---|---|---|"]
    for row in ledger:
        lines.append("| " + " | ".join(_cell(value) for value in (
            row["raw_item_id"], f"{row['date']} / {row['provenance']}", row["source"],
            row["title"], row["treatment"], row["review_depth"], row["reason"])) + " |")
    return "\n".join(lines) + "\n"


def _coverage_markdown(bundle: Bundle) -> str:
    coverage = bundle.maybe("coverage.json", [])
    runs = bundle.maybe("collection_runs.json", [])
    zero = [row["source"] for row in coverage if row["latest_status"] == "zero"]
    failed = [row for row in coverage if row["latest_status"] not in ("ok", "zero")]
    lines = ["# Source coverage", "",
             f"Every source that ran during {bundle.manifest['display_window']}, "
             f"including the ones that found nothing. {len(runs)} collection runs, "
             f"{len(coverage)} source/kind pairs.", "",
             "A source that looked and found nothing is a result, not a gap in this "
             "table. The report is allowed to say a source produced nothing.", "",
             "| Kind | Source | Runs | Statuses | Found | Latest | Error |",
             "|---|---|---:|---|---:|---|---|"]
    for row in coverage:
        statuses = ", ".join(f"{k}×{v}" for k, v in row["statuses"].items())
        lines.append("| " + " | ".join(_cell(value) for value in (
            row["kind"], row["source"], row["runs"], statuses, row["items_found_sum"],
            row["latest_status"], row["latest_error"] or "")) + " |")
    lines += ["", "## Zero-yield in the latest stored run", "",
              (", ".join(zero) or "none") + ".",
              "", "These returned zero in their latest stored discovery run during the "
              "window, which is not the same as returning zero throughout it.", ""]
    if failed:
        lines += ["## Sources whose latest run did not succeed", ""]
        lines += [f"- {row['source']} ({row['kind']}): {row['latest_status']} — "
                  f"{row['latest_error'] or 'no error recorded'}" for row in failed]
        lines.append("")
    unavailable = bundle.unavailable
    if unavailable:
        lines += ["## Titles routed for a body that never arrived", "",
                  "| ID | Source | Title | Body status | Fetch |", "|---|---|---|---|---|"]
        lines += ["| " + " | ".join(_cell(v) for v in (
            row["id"], row["source_slug"], row["title"], row.get("body_status"),
            row.get("fetch_status"))) + " |" for row in unavailable]
        lines += ["", "These are not report evidence. A title is enough to say "
                       "something was seen and unreadable; it is not enough to say "
                       "what it said.", ""]
    return "\n".join(lines) + "\n"


# ── HTML ──────────────────────────────────────────────────────────────────

CSS = """
:root{color-scheme:light;--ink:#18313b;--muted:#64737b;--accent:#166b73;--line:#dce5e8}
*{box-sizing:border-box}
body{margin:0;background:#f2f5f6;color:var(--ink);font:16px/1.85 "Segoe UI","Microsoft YaHei",sans-serif}
nav{background:#173942;color:#fff;padding:14px max(22px,calc((100vw - 1000px)/2));display:flex;gap:24px;flex-wrap:wrap;font-size:14px}
nav a{color:#fff}
main{max-width:1000px;margin:32px auto;background:#fff;padding:42px 50px;border-top:5px solid var(--accent);box-shadow:0 4px 25px #19343b08}
h1{font-size:30px;line-height:1.4;margin:0 0 22px}
h2{font-size:22px;margin:42px 0 14px;padding-top:18px;border-top:1px solid var(--line)}
h3{font-size:18px}p{margin:14px 0}
a{color:#126a79;text-underline-offset:3px;overflow-wrap:anywhere}
strong{color:#123f48}code{background:#edf2f3;padding:2px 5px;font-size:.85em;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.7;margin:20px 0}
th,td{padding:12px 14px;border:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:#eaf2f3;color:#19444a}tr:nth-child(even) td{background:#fafcfc}
ul,ol{padding-left:24px}li{margin:8px 0}.meta{font-size:13px;color:var(--muted)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;background:#f8fafb;padding:16px}
details{border:1px solid var(--line);padding:12px 16px;margin:12px 0}
summary{cursor:pointer;font-weight:600}
input{width:100%;padding:12px;font:inherit;border:1px solid #9aafb3;margin:12px 0}
footer{font-size:12px;color:var(--muted);margin-top:40px}
@media(max-width:700px){main{margin:12px;padding:24px 18px}h1{font-size:25px}h2{font-size:20px}
table{font-size:12px}th,td{padding:8px 6px}nav{gap:12px;padding:12px 18px}}
@media print{body{background:#fff;font-size:10.5pt;line-height:1.6}nav,input{display:none}
main{padding:0;margin:0;max-width:none;box-shadow:none;border:0}h1{font-size:23pt}
h2{font-size:16pt;break-after:avoid}table{font-size:9pt}tr{break-inside:avoid}a{color:inherit}
details{break-inside:avoid}}
"""


def _page(bundle: Bundle, title: str, body: str, lang: str, script: str = "") -> str:
    nav = "".join(f'<a href="{name}.html">{label}</a>' for name, label in PAGES)
    footer = (f"Brandmonitor · {bundle.client_slug} · {bundle.manifest['display_window']} "
              f"· stored evidence only, cutoff {bundle.manifest['window_end_exclusive']}")
    return ('<!doctype html>\n<html lang="' + lang + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>" + html.escape(title) + "</title><style>" + CSS + "</style></head>"
            "<body><nav>" + nav + "</nav><main>" + body +
            "<footer>" + html.escape(footer) + "</footer></main>" + script + "</body></html>")


def _inline(text: str) -> str:
    text = html.escape(text)
    def link(match: re.Match[str]) -> str:
        url = match.group(2)
        if url.endswith(".md"):
            url = url[:-3] + ".html"
        return f'<a href="{url}">{match.group(1)}</a>'
    text = MD_LINK.sub(link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _markdown_to_html(md: str) -> str:
    """The subset this project writes: headings, tables, lists, paragraphs."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
            i += 1
        elif line.startswith("|"):
            table: list[list[str]] = []
            while i < len(lines) and lines[i].startswith("|"):
                table.append([cell.strip() for cell in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>"
                       + "".join(f"<th>{_inline(c)}</th>" for c in table[0])
                       + "</tr></thead><tbody>")
            for row in table[2:]:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table>")
        elif line.startswith("- ") or re.match(r"^\d+\. ", line):
            ordered = bool(re.match(r"^\d+\. ", line))
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>")
            while i < len(lines) and (bool(re.match(r"^\d+\. ", lines[i])) if ordered
                                      else lines[i].startswith("- ")):
                out.append("<li>" + _inline(re.sub(r"^(?:- |\d+\. )", "", lines[i])) + "</li>")
                i += 1
            out.append(f"</{tag}>")
        else:
            paragraph = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ")):
                paragraph.append(lines[i])
                i += 1
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
    return "\n".join(out)


def _evidence_page(bundle: Bundle, ledger: list[dict[str, Any]]) -> str:
    """The frozen reader the ledger links into: one record per identity."""
    decisions = {row["raw_item_id"]: row for row in ledger}
    rows = bundle.by_id
    body = ["<h1>Stored evidence reader</h1>",
            "<p>Frozen local copies as stored at collection time, not a fresh check of "
            "the publisher sites. Filter by id, title or source; open a record for its "
            "dates, its editorial decision and its stored text.</p>",
            '<label for="filter">Filter records</label>'
            '<input id="filter" type="search" placeholder="24617, Hermes, Safety Gate…">']
    for raw_id in sorted(rows):
        row = rows[raw_id]
        payload = row.get("payload") or {}
        meta = {k: row.get(k) for k in ("id", "version", "source_slug", "source_kind",
                                        "published_at", "fetched_at", "content_hash",
                                        "date_provenance", "event_or_publication_day",
                                        "route")}
        meta["editorial_decision"] = decisions.get(raw_id)
        text = payload.get("body_text") or json.dumps(payload, ensure_ascii=False, indent=2)
        url = row.get("url") or ""
        body.append(
            f'<details id="item-{raw_id}"><summary>{raw_id} · '
            + html.escape(row.get("title") or row.get("external_id") or "")
            + f' <span class="meta">{html.escape(row["source_slug"])}</span></summary>'
            + (f'<p><a href="{html.escape(url)}">Publisher source</a></p>' if url else "")
            + "<pre>" + html.escape(json.dumps(meta, ensure_ascii=False, indent=2))
            + "</pre><pre>" + html.escape(text) + "</pre></details>")
    script = ("<script>const f=document.getElementById('filter');"
              "f.addEventListener('input',()=>{const q=f.value.toLowerCase();"
              "document.querySelectorAll('details').forEach(d=>d.hidden="
              "!d.querySelector('summary').textContent.toLowerCase().includes(q));});"
              "</script>")
    return _page(bundle, "Stored evidence reader", "\n".join(body), "en", script)


def _readme(bundle: Bundle, register: list[dict[str, Any]],
            ledger: list[dict[str, Any]]) -> str:
    manifest = bundle.manifest
    assessment = bundle.maybe("assessment-manifest.json", {})
    read_fully = sum(1 for row in ledger if row["review_depth"] == "full_stored_body")
    return "\n".join([
        f"# {manifest.get('client_name', bundle.client_slug)} · "
        f"{manifest['display_window']}", "",
        "Start with the [Chinese weekly report](weekly-report.zh.html).", "",
        f"- [Editorial ledger](editorial-ledger.html): what happened to each of "
        f"{len(ledger)} identities, and how deeply each was read "
        f"({read_fully} bodies opened in full).",
        "- [Source coverage](source-coverage.html): every source that ran, including "
        "the ones that found nothing.",
        "- [Stored evidence](source-evidence.html): the frozen titles and bodies.",
        f"- `issue-register.json`: {len(register)} issues with status, evidence and the "
        "trigger for the next update.",
        "- `manifest.json`: window, snapshot, counts, stated policy, input hashes.",
        "- `decisions.json`, `tool-calls.jsonl`, `assessment-manifest.json`: the "
        "assessment's output, what it read, and under which prompt versions.", "",
        f"Model: `{assessment.get('model', 'not recorded')}`. "
        f"Profile version: `{manifest['profile_version']}`.", "",
        "Everything here is local and offline. To re-render after an edit to the "
        "assessment, run `python run.py report --bundle "
        f"data/reports/{bundle.path.name}`; to check it, "
        f"`python run.py verify-report --bundle data/reports/{bundle.path.name}`.", "",
        "The frozen half of this directory is written once by `export-window` and is "
        "not modified by any later stage.", ""])


# ── helpers ───────────────────────────────────────────────────────────────

def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def _tally(values: Iterable[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _title_of(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Brandmonitor"
