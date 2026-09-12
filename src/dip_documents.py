"""Documents behind the steps of DIP procedures a client's gate kept.

A procedure's record says what happened; its documents say what it means. The
record of the written question on PFAS in Shein textiles names the question. The
government's answer in the collective Drucksache adds that customs fees on parcels
from outside the EU apply from 1 July 2026 and the 150-euro threshold is gone. So
for every procedure the regulatory body gate judged relevant for a client, the
Drucksachen its steps reference are fetched from DIP's ``/drucksache-text`` and
stored whole; what the full assessment reads is cut from them when it is read.

Only ``relevant`` fetches. On the first pass none of the 7 DIP procedures the gate
left unsure carried anything for the client in its documents
(docs/selection_and_assessment.md, "Parliamentary procedures").

What is fetched, measured 2026-09-11 over the 1,656 stored procedures
(docs/source_coverage.md, "Parliament"):

  * every Drucksache a kept procedure's newest version references, once per DIP
    document id and shared by every client. The gate decides once per procedure,
    so a step added after its decision - the government's answer - is fetched too;
  * not plenary protocols. A step there is a passage in the minutes of a whole
    sitting (692,156 characters for one), and on the procedures kept so far it
    was a committee's one-line "taken note" notice;
  * not list entries. A Drucksache step with a question number outside the
    written questions is one line in a list: EU documents referred to committees
    (``Überweisung gemäß § 93``), or oral questions answered in the protocol.

Documents run from 900 characters to 1.1 million, so the cut depends on type:

  * a written question: the one question with its answer, cut out of the
    collective Drucksache by ``frage_nummer`` - 750 to 4,500 characters out of up
    to 627,020;
  * a bill, an ordinance, a committee report: the summary each opens with, from
    "A. Problem" to the draft or the committee's recommendation - 5,400 to 6,800
    characters for three bills of 154,000 to 1,112,000;
  * anything else - an answer to a minor question, a motion, a report - from the
    start, up to DOCUMENT_CHARS, saying so when it is cut.

A bill's summary does not always carry what matters to the client. The customs
bill's says nothing about parcels, while one paragraph among its 1.1 million
characters obliges parcel carriers to give customs investigators shipment data.
Finding such passages is not built; see todo.md.

DIP publishes a Drucksache's text some days after the document. On 2026-09-11
every Drucksache dated that day, and an answer dated 09-08, came back without
text. So a missing text keeps the document ``pending`` for the next run, and like
an HTTP failure it is given up after MAX_ATTEMPTS consecutive runs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from src.body_gate import STAGE as GATE_STAGE
from src.collect import collector_entry, slug_for
from src.db import finish_run, record_source_result, session, start_run, utcnow
from src.dip import COLLECTOR, DipClient, DipError, DipKeyError, step_order
from src.logger import get_logger

logger = get_logger(__name__)

RUN_KIND = "dip_documents"
# The regulatory body gate's decisions; a procedure is fetched for while the
# client's latest one is relevant.
GATE_PREFIX = f"{GATE_STAGE}-regulatory-"
DEFAULT_LIMIT = 50
# Consecutive runs without text, or failing, before a document is given up.
# DIP's text follows a document by days; two weeks of daily runs is a guard
# rail, and --retry-unavailable reopens.
MAX_ATTEMPTS = 14
WRITTEN_QUESTIONS = "Schriftliche Fragen"

# What the full assessment reads: per question, per summary, per other
# document, and per procedure across its documents.
QUESTION_CHARS = 10_000
SUMMARY_CHARS = 10_000
DOCUMENT_CHARS = 30_000
EXCERPT_CHARS = 40_000


# ── which documents ───────────────────────────────────────────────────────

def fetchable(fundstelle: Optional[dict[str, Any]]) -> bool:
    """A Drucksache of its own, not a line in the minutes or in a list."""
    if not fundstelle or fundstelle.get("dokumentart") != "Drucksache" or not fundstelle.get("id"):
        return False
    return not fundstelle.get("frage_nummer") or fundstelle.get("drucksachetyp") == WRITTEN_QUESTIONS


def documents_of(positions: Sequence[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """(step, fundstelle) for each document worth fetching, newest step first."""
    return [(step, step["fundstelle"])
            for step in sorted(positions, key=step_order, reverse=True)
            if fetchable(step.get("fundstelle"))]


def kept_procedures(conn: sqlite3.Connection, client_slug: str,
                    slug: str) -> dict[str, dict[str, Any]]:
    """The newest payload of each procedure whose latest gate decision is relevant."""
    decisions: dict[str, Optional[int]] = {}
    for row in conn.execute(
            "SELECT r.external_id, a.relevant FROM assessment a "
            "JOIN raw_item r ON r.id = a.raw_item_id "
            "WHERE a.client_slug = ? AND r.source_slug = ? "
            "AND substr(a.prompt_version, 1, ?) = ? ORDER BY a.created_at, a.id",
            (client_slug, slug, len(GATE_PREFIX), GATE_PREFIX)):
        decisions[row["external_id"]] = row["relevant"]
    kept = {external_id for external_id, relevant in decisions.items() if relevant == 1}
    payloads: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT external_id, payload FROM raw_item WHERE source_slug = ? "
                            "ORDER BY external_id, version", (slug,)):
        if row["external_id"] in kept:
            payloads[row["external_id"]] = json.loads(row["payload"])
    return payloads


def _queue(conn: sqlite3.Connection, payloads: Iterable[dict[str, Any]]) -> int:
    added = 0
    for payload in payloads:
        for _step, source in documents_of(payload.get("positionen") or []):
            added += conn.execute(
                "INSERT OR IGNORE INTO dip_document (document_id, dokumentnummer, "
                "drucksachetyp, herausgeber, datum, pdf_url) VALUES (?, ?, ?, ?, ?, ?)",
                (str(source["id"]), source.get("dokumentnummer"), source.get("drucksachetyp"),
                 source.get("herausgeber"), source.get("datum"), source.get("pdf_url")),
            ).rowcount
    return added


# ── fetch ─────────────────────────────────────────────────────────────────

def run_document_fetch(client_slug: str, *, limit: int = DEFAULT_LIMIT,
                       retry_unavailable: bool = False, client: Optional[DipClient] = None,
                       db_path: Optional[Path] = None,
                       sources_path: Optional[Path] = None) -> dict[str, Any]:
    """Queue the documents of the client's kept procedures and fetch what is due.

    The queue owns retries: a document once queued is asked for until it has its
    text or is given up, whoever's decision queued it. Newest documents first.
    A DipKeyError closes the run as failed and is raised again.
    """
    slug = slug_for(collector_entry(COLLECTOR, sources_path))
    today = date.today().isoformat()
    summary: dict[str, Any] = {"kind": RUN_KIND, "client": client_slug, "kept": 0,
                               "queued": 0, "reopened": 0, "attempted": 0, "ok": 0,
                               "no_text": 0, "failed": 0, "unavailable": 0, "deferred": 0,
                               "errors": []}
    with session(db_path) as conn:
        kept = kept_procedures(conn, client_slug, slug)
        summary["kept"] = len(kept)
        summary["queued"] = _queue(conn, kept.values())
        if retry_unavailable:
            summary["reopened"] = conn.execute(
                "UPDATE dip_document SET status = 'pending', attempts = 0 "
                "WHERE status = 'unavailable'").rowcount
        due = [row["document_id"] for row in conn.execute(
            "SELECT document_id FROM dip_document WHERE status IN ('pending', 'failed') "
            "ORDER BY datum DESC, document_id DESC")]
        run_id = start_run(conn, RUN_KIND, today, today)
    summary["run_id"] = run_id
    summary["deferred"] = max(0, len(due) - limit)

    api = client or DipClient()
    fatal: Optional[DipKeyError] = None
    try:
        for document_id in due[:limit]:
            summary["attempted"] += 1
            try:
                data = api.document_text(document_id)
            except DipKeyError:
                raise
            except DipError as exc:
                logger.warning("[dip documents] %s: %s", document_id, exc)
                outcome, text, error = "failed", None, str(exc)
                summary["errors"].append(f"{document_id}: {exc}")
            else:
                text = (data.get("text") or "").strip() or None
                outcome, error = ("ok", None) if text else ("no_text", "DIP has no text for it yet")
            # Committed per document, so a stopped run keeps what it fetched.
            with session(db_path) as conn:
                summary[_record(conn, run_id, document_id, outcome, text, error)] += 1
    except DipKeyError as exc:
        fatal = exc
        summary["errors"].append(str(exc))

    with session(db_path) as conn:
        failed = bool(fatal) or (summary["failed"] and not summary["ok"])
        status = "failed" if failed else ("ok" if summary["ok"] else "zero")
        record_source_result(conn, run_id, slug, status, items_found=summary["attempted"],
                             items_stored=summary["ok"],
                             error="; ".join(summary["errors"][:3]) or None)
        summary["remaining"] = dict.fromkeys(("pending", "ok", "failed", "unavailable"), 0)
        summary["remaining"].update({row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM dip_document GROUP BY status")})
        finish_run(conn, run_id, "failed" if failed else "ok",
                   note=(f"{summary['kept']} kept procedures; {summary['attempted']} documents "
                         f"attempted, {summary['ok']} stored, {summary['no_text']} without text"))
    if fatal is not None:
        raise fatal
    return summary


def _record(conn: sqlite3.Connection, run_id: int, document_id: str, outcome: str,
            text: Optional[str], error: Optional[str]) -> str:
    """Store one attempt; return the summary counter it belongs to."""
    now = utcnow()
    if outcome == "ok":
        conn.execute("UPDATE dip_document SET status = 'ok', text = ?, fetched_at = ?, "
                     "attempts = 0, attempted_at = ?, error = NULL, last_run_id = ? "
                     "WHERE document_id = ?", (text, now, now, run_id, document_id))
        return "ok"
    attempts = conn.execute("SELECT attempts FROM dip_document WHERE document_id = ?",
                            (document_id,)).fetchone()["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        status, counter, error = "unavailable", "unavailable", f"gave up after {attempts} attempts: {error}"
    else:
        status, counter = ("pending" if outcome == "no_text" else "failed"), outcome
    conn.execute("UPDATE dip_document SET status = ?, attempts = ?, attempted_at = ?, "
                 "error = ?, last_run_id = ? WHERE document_id = ?",
                 (status, attempts, now, error, run_id, document_id))
    return counter


# ── the cut ───────────────────────────────────────────────────────────────

NOISE = (
    # On every page of a Bundestag advance copy, ending in "Version" or "Fassung";
    # the PDF breaks lines inside "Vorabfassung", "wird" and "Version".
    re.compile(r"V\s*orabfassung\s*[-–]\s*w\s*ird\s+durch\s+die\s+lektorierte\s+"
               r"(?:V\s*ersion|F\s*assung)\s+ersetzt\.[ \t]*\n?"),
    # The printer's and distributor's imprint, heading a Bundesrat Drucksache and
    # closing a Bundestag one.
    re.compile(r"Gesamtherstellung: [^\n]*\n?"),
    re.compile(r"Vertrieb: Bundesanzeiger Verlag GmbH.{0,300}?ISSN \d{4}-\d{3}[\dX][ \t]*\n?",
               re.DOTALL),
)
ANY_QUESTION = re.compile(r"(?m)^[ \t]*\d{1,4}\.\s+Abgeordnete")
SUMMARY_START = re.compile(r"A\.\s*Problem")
SUMMARY_SOLUTION = re.compile(r"B\.\s*Lösung")
# The last heading of a bill's or an ordinance's summary; a committee report's is D.
SUMMARY_LAST = re.compile(r"F\.\s*Weitere Kosten")
# What follows a summary: the draft, a committee's recommendation, or in a
# Bundesrat Drucksache its header printed again above the cover letter - the only
# marker an ordinance has, since its draft is no "Entwurf eines".
SUMMARY_END = re.compile(r"Entwurf eines|Der Bundestag wolle beschließen|Bundesrat\s+Drucksache")
REPORT_START = re.compile(r"Bericht der Abgeordneten")
# A summary opens its document; an "A. Problem" further in is quoted from another.
SUMMARY_WITHIN = 15_000
# A committee report's point is what it recommends, which follows its summary.
RECOMMENDATION_CHARS = 2_000


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for pattern in NOISE:
        text = pattern.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def question_numbers(frage_nummer: Optional[str]) -> list[str]:
    """``"54, 55, 56"`` -> ``["54", "55", "56"]``: one step can cover several."""
    return re.findall(r"\d+", frage_nummer or "")


def written_question(text: str, number: str) -> Optional[str]:
    """One question and its answer: from its number to the next question's."""
    start = re.search(rf"(?m)^[ \t]*{re.escape(number)}\.\s+Abgeordnete", text)
    if start is None:
        return None
    following = ANY_QUESTION.search(text, start.end())
    return text[start.start():following.start() if following else len(text)].strip()


def summary(text: str) -> Optional[str]:
    """The "A. Problem ... B. Lösung ..." summary a bill, ordinance or report opens with."""
    start = SUMMARY_START.search(text, 0, SUMMARY_WITHIN)
    if start is None:
        return None
    solution = SUMMARY_SOLUTION.search(text, start.end())
    if solution is None:
        return None
    # The end is looked for after the last heading, so a draft the summary names
    # ("Mit dem Entwurf eines Gesetzes ...") is not taken for the draft itself.
    last = SUMMARY_LAST.search(text, solution.end(), start.start() + SUMMARY_WITHIN)
    end = SUMMARY_END.search(text, (last or solution).end())
    if end is None:
        stop = len(text)
    elif end.group(0).startswith("Der Bundestag"):
        report = REPORT_START.search(text, end.end(), end.end() + RECOMMENDATION_CHARS)
        stop = report.start() if report else end.end() + RECOMMENDATION_CHARS
    else:
        stop = end.start()
    return text[start.start():stop].strip()


def cut(text: str, drucksachetyp: Optional[str], frage_nummer: Optional[str] = None) -> str:
    """What the full assessment reads of one document. Says so when that is not all of it."""
    text = clean(text)
    if drucksachetyp == WRITTEN_QUESTIONS and frage_nummer:
        parts, missing = [], []
        for number in question_numbers(frage_nummer):
            found = written_question(text, number)
            if found:
                parts.append(found[:QUESTION_CHARS])
            else:
                missing.append(number)
        if missing:
            parts.append(f"[question {', '.join(missing)} not found in the document]")
        return "\n\n".join(parts)
    opening = summary(text)
    if opening is not None:
        return (f"{opening[:SUMMARY_CHARS]}\n[the document's opening summary; the whole "
                f"document has {len(text):,} characters]")
    if len(text) <= DOCUMENT_CHARS:
        return text
    return f"{text[:DOCUMENT_CHARS]}\n[first {DOCUMENT_CHARS:,} of {len(text):,} characters]"


def _label(step: dict[str, Any], source: dict[str, Any]) -> str:
    label = (f"{step.get('datum') or '?'} {step.get('vorgangsposition') or ''} - Drucksache "
             f"{source.get('dokumentnummer') or source['id']} ({source.get('drucksachetyp') or '?'})")
    if source.get("frage_nummer"):
        label += f", Frage {source['frage_nummer']}"
    return label


def procedure_excerpt(conn: sqlite3.Connection, positions: Sequence[dict[str, Any]], *,
                      budget: int = EXCERPT_CHARS) -> str:
    """A procedure's cut documents, newest step first, within ``budget`` characters.

    With the record's own body this is what the full assessment reads. A document
    not fetched yet is still named, so the reader knows the answer exists.
    """
    documents = documents_of(positions)
    ids = sorted({str(source["id"]) for _step, source in documents})
    rows: dict[str, sqlite3.Row] = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = {row["document_id"]: row for row in conn.execute(
            f"SELECT document_id, status, error, text FROM dip_document "
            f"WHERE document_id IN ({marks})", ids)}
    blocks: list[str] = []
    seen: set[tuple[str, Optional[str]]] = set()
    used = 0
    for step, source in documents:
        key = (str(source["id"]), source.get("frage_nummer"))
        if key in seen:
            continue
        seen.add(key)
        row = rows.get(key[0])
        if row is not None and row["status"] == "ok":
            text = cut(row["text"], source.get("drucksachetyp"), source.get("frage_nummer"))
        elif row is not None and row["status"] == "unavailable":
            text = f"[not available: {row['error']}]"
        else:
            text = "[not fetched yet]"
        block = f"=== {_label(step, source)} ===\n{text}"
        if blocks and used + len(block) > budget:
            left = len({(str(s["id"]), s.get("frage_nummer")) for _st, s in documents} - seen) + 1
            blocks.append(f"[{left} older document(s) left out]")
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def show_procedure(procedure_id: str, *, db_path: Optional[Path] = None,
                   sources_path: Optional[Path] = None) -> Optional[str]:
    """The newest record of a procedure and its cut documents, as the assessment will read them."""
    slug = slug_for(collector_entry(COLLECTOR, sources_path))
    with session(db_path) as conn:
        row = conn.execute("SELECT title, payload FROM raw_item WHERE source_slug = ? AND "
                           "external_id = ? ORDER BY version DESC LIMIT 1",
                           (slug, str(procedure_id))).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        documents = procedure_excerpt(conn, payload.get("positionen") or [])
    return (f"{row['title']}\n\n{payload.get('body_text') or ''}\n\n--- documents ---\n"
            f"{documents or '(none referenced)'}")
