"""DIP documents: what is fetched for whom, the retry queue, and the cut."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.db import migrate, session, start_run
from src.dip import DipError, DipKeyError, store_procedure
from src.dip_documents import (
    DOCUMENT_CHARS, MAX_ATTEMPTS, cut, documents_of, fetchable, procedure_excerpt,
    run_document_fetch, show_procedure,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dip_procedures.json")
                     .read_text(encoding="utf-8"))
SLUG = "dip.bundestag.de"
CLIENT = "jt-express"
GATE = "body_gate-regulatory-0123456789ab"
QUESTION, MINOR_QUESTION, RESOLUTION = "334627", "337163", "322138"
DIP_ENTRY = {"url": "https://dip.bundestag.de/", "organization": "DIP", "collector": "dip",
             "content_mode": "full_text", "access": "free"}


def procedure(pid: str) -> dict:
    return copy.deepcopy(next(p for p in FIXTURE["vorgang"] if p["id"] == pid))


def steps(pid: str) -> list[dict]:
    return copy.deepcopy([s for s in FIXTURE["vorgangsposition"] if s["vorgang_id"] == pid])


@pytest.fixture()
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    sources = tmp_path / "regulatory.json"
    sources.write_text(json.dumps([DIP_ENTRY]), encoding="utf-8")
    return db, sources


def store(db, record: dict, course: list[dict]) -> int:
    """Store a procedure version and return its raw_item id."""
    with session(db) as conn:
        store_procedure(conn, start_run(conn, "dip", "a", "b"), SLUG, record, course)
        return conn.execute("SELECT MAX(id) FROM raw_item WHERE external_id = ?",
                            (str(record["id"]),)).fetchone()[0]


def decide(db, raw_item_id: int, relevant, *, client: str = CLIENT, version: str = GATE,
           when: str = "2026-09-11T06:00:00+00:00") -> None:
    with session(db) as conn:
        conn.execute("INSERT INTO assessment (raw_item_id, client_slug, prompt_version, "
                     "profile_version, created_at, relevant, payload) "
                     "VALUES (?, ?, ?, 'p1', ?, ?, '{}')",
                     (raw_item_id, client, version, when, relevant))


class FakeDip:
    def __init__(self, texts=None, *, errors=None, key_error=False):
        self.texts = texts or {}
        self.errors = errors or {}
        self.key_error = key_error
        self.calls: list[str] = []

    def document_text(self, document_id):
        self.calls.append(document_id)
        if self.key_error:
            raise DipKeyError("drucksache-text: HTTP 401 - DIP rejected the API key")
        if document_id in self.errors:
            raise DipError(self.errors[document_id])
        return {"id": document_id, "text": self.texts.get(document_id, "")}


def fetch(project, api, **kwargs):
    db, sources = project
    return run_document_fetch(CLIENT, client=api, db_path=db, sources_path=sources, **kwargs)


def document(db, document_id):
    with session(db) as conn:
        return conn.execute("SELECT * FROM dip_document WHERE document_id = ?",
                            (document_id,)).fetchone()


# ── which documents ───────────────────────────────────────────────────────

def test_plenary_protocols_and_list_entries_are_not_documents():
    by_id = {s["fundstelle"]["id"]: s["fundstelle"] for s in FIXTURE["vorgangsposition"]}
    assert not fetchable(by_id["5716"])     # a passage in the minutes of a sitting
    assert fetchable(by_id["287151"])       # a written question, found by its number
    assert fetchable(by_id["289930"])       # the government's answer
    referral = dict(by_id["289930"], drucksachetyp="Unterrichtung", frage_nummer="A.2")
    assert not fetchable(referral)          # one line in a list of EU documents referred
    assert not fetchable(None)


def test_documents_come_newest_step_first():
    listed = [source["id"] for _step, source in documents_of(steps(RESOLUTION))]
    # The government's reply, the Bundesrat's resolution, its committees, the motion;
    # the two sittings in between are plenary protocols.
    assert listed == ["289865", "280424", "280172", "279646"]


# ── what is fetched for whom ──────────────────────────────────────────────

def test_only_a_relevant_decision_for_this_client_fetches(project):
    db, _ = project
    decide(db, store(db, procedure(QUESTION), steps(QUESTION)), 1)
    decide(db, store(db, procedure(MINOR_QUESTION), steps(MINOR_QUESTION)), None)  # unsure
    decide(db, store(db, procedure(RESOLUTION), steps(RESOLUTION)), 1, client="other-client")
    api = FakeDip({"287151": "9. Abgeordneter\nFrage?\nAntwort."})
    s = fetch(project, api)
    assert api.calls == ["287151"]
    assert (s["kept"], s["queued"], s["attempted"], s["ok"]) == (1, 1, 1, 1)


def test_the_latest_decision_counts(project):
    db, _ = project
    raw = store(db, procedure(QUESTION), steps(QUESTION))
    decide(db, raw, 1, version="body_gate-regulatory-older", when="2026-09-10T06:00:00+00:00")
    decide(db, raw, 0)  # re-gated since: irrelevant
    api = FakeDip()
    assert fetch(project, api)["kept"] == 0 and api.calls == []


def test_a_document_is_stored_whole_and_fetched_once_for_every_procedure(project):
    db, _ = project
    decide(db, store(db, procedure(QUESTION), steps(QUESTION)), 1)
    # Another written question, answered in the same collective Drucksache.
    other = dict(procedure(QUESTION), id="999")
    other_steps = [dict(s, vorgang_id="999", fundstelle=dict(s["fundstelle"], frage_nummer="12"))
                   for s in steps(QUESTION)]
    decide(db, store(db, other, other_steps), 1)
    collective = "Schriftliche Fragen\n" + "x" * 200_000
    api = FakeDip({"287151": collective})
    fetch(project, api)
    assert api.calls == ["287151"]
    row = document(db, "287151")
    assert (row["status"], row["dokumentnummer"], row["text"]) == ("ok", "21/5580", collective)
    assert fetch(project, FakeDip())["attempted"] == 0


def test_a_step_added_after_the_decision_is_fetched_too(project):
    db, _ = project
    record, course = procedure(MINOR_QUESTION), steps(MINOR_QUESTION)
    decide(db, store(db, record, course[:1]), 1)  # gated while only the question existed
    api = FakeDip({"289634": "Kleine Anfrage", "289930": "Antwort der Bundesregierung"})
    fetch(project, api)
    store(db, record, course)  # the answer arrives as version 2; the decision stands
    fetch(project, api)
    assert api.calls == ["289634", "289930"]


def test_limit_takes_the_newest_documents_first(project):
    db, _ = project
    decide(db, store(db, procedure(RESOLUTION), steps(RESOLUTION)), 1)
    api = FakeDip(dict.fromkeys(("289865", "280424", "280172", "279646"), "text"))
    s = fetch(project, api, limit=2)
    assert api.calls == ["289865", "280424"] and s["deferred"] == 2


# ── retries ───────────────────────────────────────────────────────────────

def test_a_document_without_text_waits_for_the_next_run(project):
    db, _ = project
    decide(db, store(db, procedure(QUESTION), steps(QUESTION)), 1)
    s = fetch(project, FakeDip({"287151": ""}))
    assert (s["attempted"], s["no_text"], s["failed"]) == (1, 1, 0)
    row = document(db, "287151")
    assert (row["status"], row["attempts"], row["text"]) == ("pending", 1, None)
    fetch(project, FakeDip({"287151": "9. Abgeordneter\nAntwort."}))
    row = document(db, "287151")
    assert (row["status"], row["attempts"], row["error"]) == ("ok", 0, None)


def test_gives_up_after_max_attempts_until_reopened(project):
    db, _ = project
    decide(db, store(db, procedure(QUESTION), steps(QUESTION)), 1)
    for _ in range(MAX_ATTEMPTS):
        fetch(project, FakeDip())
    row = document(db, "287151")
    assert row["status"] == "unavailable"
    assert row["error"].startswith(f"gave up after {MAX_ATTEMPTS} attempts")
    idle = FakeDip({"287151": "text"})
    fetch(project, idle)
    assert idle.calls == []
    s = fetch(project, idle, retry_unavailable=True)
    assert (s["reopened"], s["ok"]) == (1, 1)


def test_http_failure_is_retried_and_a_rejected_key_aborts(project):
    db, _ = project
    decide(db, store(db, procedure(QUESTION), steps(QUESTION)), 1)
    s = fetch(project, FakeDip(errors={"287151": "drucksache-text/287151: HTTP 503"}))
    assert (s["failed"], document(db, "287151")["status"]) == (1, "failed")
    with pytest.raises(DipKeyError):
        fetch(project, FakeDip(key_error=True))
    with session(db) as conn:
        last = conn.execute("SELECT status FROM run WHERE kind = 'dip_documents' "
                            "ORDER BY id DESC").fetchone()
    assert last["status"] == "failed"
    assert document(db, "287151")["attempts"] == 1  # the aborted run recorded nothing


# ── the cut ───────────────────────────────────────────────────────────────

COLLECTIVE = """Deutscher Bundestag Drucksache 21/5580
Schriftliche Fragen mit den in der Woche vom 20. April 2026 eingegangenen Antworten
8. Abgeordneter
Max Muster
(CDU/CSU)
Wie viele Brücken sind marode?
Antwort des Staatssekretärs Hans Beispiel
Zahlreiche.
Geschäftsbereich des Bundesministeriums der Finanzen
9. Abgeordneter
Dr. Rainer Kraft
(AfD)
Wie hoch ist die Kontrollquote bei E-Commerce-Sendungen aus dem Ausland?
Vorabfassung - w
ird durch die lektorierte Version ersetzt.
Antwort des Parlamentarischen Staatssekretärs Michael Schrodi
Sie lag 2025 bei 0,1 Prozent der Sendungen.
10. Abgeordnete
Erika Beispiel
(SPD)
Wann kommt der Bericht?
Antwort: bald.
"""


def test_a_written_question_is_cut_out_of_its_collective_drucksache():
    text = cut(COLLECTIVE, "Schriftliche Fragen", "9")
    assert text.startswith("9. Abgeordneter\nDr. Rainer Kraft")
    assert "0,1 Prozent" in text
    assert "Brücken" not in text and "Wann kommt" not in text
    assert "Vorabfassung" not in text and "lektorierte" not in text
    both = cut(COLLECTIVE, "Schriftliche Fragen", "8, 9")
    assert "Brücken" in both and "0,1 Prozent" in both and "Wann kommt" not in both
    assert "question 42 not found" in cut(COLLECTIVE, "Schriftliche Fragen", "42")


BILL = """Bundesrat Drucksache 446/26
Gesetzentwurf der Bundesregierung
Entwurf eines Gesetzes für mehr Gerechtigkeit durch die Stärkung der Zollverwaltung
A. Problem und Ziel
Die Zollverwaltung soll weiterentwickelt werden.
B. Lösung
Mit dem Entwurf eines Gesetzes wird die Generalzolldirektion neu strukturiert.
C. Alternativen
Keine.
F. Weitere Kosten
Keine.
Bundesrat Drucksache 446/26
14.08.26
Gesetzentwurf der Bundesregierung
Bundesrepublik Deutschland Der Bundeskanzler
hiermit übersende ich den Entwurf eines Gesetzes mit Begründung und Vorblatt.
Entwurf eines Gesetzes für mehr Gerechtigkeit durch die Stärkung der Zollverwaltung
Artikel 1
§ 72 Absatz 1a: Auskunft über Postsendungen von Unternehmen, die Postdienste erbringen.
""" + "Begründung\n" + "y" * 100_000


def test_a_bill_is_read_by_its_opening_summary():
    text = cut(BILL, "Gesetzentwurf")
    assert text.startswith("A. Problem und Ziel")
    assert "Mit dem Entwurf eines Gesetzes wird" in text  # a draft the summary names
    assert text.split("\n[")[0].endswith("F. Weitere Kosten\nKeine.")
    assert "Bundeskanzler" not in text and "Postsendungen" not in text
    assert f"the whole document has {len(BILL.strip()):,} characters" in text


ORDINANCE = """Bundesrat Drucksache 512/26
Verordnung des Bundesministeriums der Finanzen
A. Problem und Ziel
Die Schlüsselzahlen sind neu festzusetzen.
B. Lösung
Erlass der Verordnung.
F. Weitere Kosten
Weitere Kosten, insbesondere für die Wirtschaft, entstehen nicht.
Bundesrat Drucksache 512/26
03.09.26
Bundeskanzleramt Berlin, 1. September 2026
Verordnung über die Festsetzung der Länderschlüsselzahlen
Vom ...
Das Bundesministerium der Finanzen verordnet:
""" + "§ 1\n" + "v" * 30_000


def test_an_ordinance_summary_stops_where_its_header_repeats():
    # An ordinance's draft is no "Entwurf eines"; the Bundesrat header repeated
    # above the cover letter is the only end marker it has.
    text = cut(ORDINANCE, "Verordnung")
    assert text.split("\n[")[0].endswith("entstehen nicht.")
    assert "Bundeskanzleramt" not in text and "verordnet" not in text


COMMITTEE = """Deutscher Bundestag Drucksache 21/7942
Beschlussempfehlung und Bericht des Ausschusses für Wohnen
A. Problem
Die Mieten steigen.
B. Lösung
Ablehnung des Antrags.
C. Alternativen
Keine.
D. Kosten
Wurden nicht erörtert.
V
orabfassung – w
ird durch die lektorierte Fassung ersetzt.
Beschlussempfehlung
Der Bundestag wolle beschließen,
den Antrag auf Drucksache 21/1234 abzulehnen.
Berlin, den 9. September 2026
Der Ausschuss für Wohnen
Bericht der Abgeordneten Anna Beispiel und Ben Muster
I. Überweisung
""" + "z" * 50_000


def test_a_committee_report_keeps_its_recommendation_and_not_the_report():
    text = cut(COMMITTEE, "Beschlussempfehlung und Bericht")
    assert text.startswith("A. Problem") and "abzulehnen" in text
    assert "Bericht der Abgeordneten" not in text and "Überweisung" not in text
    assert "orabfassung" not in text  # the advance-copy line, in its other wording


def test_other_documents_are_read_from_the_start_up_to_a_cap():
    imprint = ("Vertrieb: Bundesanzeiger Verlag GmbH, Postfach 10 05 34, 50445 Köln\n"
               "Telefon (02 21) 97 66 83 40, www.bundesanzeiger-verlag.de\nISSN 0720-2946\n")
    motion = "Bundesrat Drucksache 525/26\nAntrag des Landes Hessen\nEntschließung."
    assert cut(imprint + motion, "Antrag") == motion
    reply = "Antwort der Bundesregierung\nDie Kontrollquote liegt bei 0,1 Prozent."
    closing = ("\nGesamtherstellung: H. Heenemann GmbH & Co. KG, 12103 Berlin\n"
               "Vertrieb: Bundesanzeiger Verlag GmbH, Postfach 10 05 34, 50445 Köln\n"
               "ISSN 0722-8333")
    assert cut(reply + closing, "Antwort") == reply
    answer = "Antwort der Bundesregierung\n" + "a" * (DOCUMENT_CHARS * 2)
    text = cut(answer, "Antwort")
    assert text.startswith("Antwort der Bundesregierung")
    assert f"first {DOCUMENT_CHARS:,} of" in text and len(text) < DOCUMENT_CHARS + 100


# ── what the assessment reads ─────────────────────────────────────────────

def test_excerpt_names_each_step_newest_first_and_what_is_missing(project):
    db, _ = project
    course = steps(RESOLUTION)
    store(db, procedure(RESOLUTION), course)
    with session(db) as conn:
        conn.execute("INSERT INTO dip_document (document_id, status, text) VALUES "
                     "('289865', 'ok', 'Stellungnahme der Bundesregierung: die Zollfreigrenze "
                     "fällt.'), ('280424', 'pending', NULL)")
        excerpt = procedure_excerpt(conn, course)
        short = procedure_excerpt(conn, course, budget=100)
    blocks = excerpt.split("\n\n")
    assert blocks[0] == ("=== 2026-07-16 Unterrichtung - Drucksache zu228/25(B) (Unterrichtung) "
                         "===\nStellungnahme der Bundesregierung: die Zollfreigrenze fällt.")
    assert blocks[1].startswith("=== 2025-07-11 Beschlussdrucksache - Drucksache 228/25(B)")
    assert blocks[1].endswith("[not fetched yet]")
    assert len(blocks) == 4 and "Plenarprotokoll" not in excerpt
    assert short.startswith(blocks[0]) and short.endswith("[3 older document(s) left out]")


def test_show_prints_the_record_and_its_documents(project):
    db, sources = project
    store(db, procedure(QUESTION), steps(QUESTION))
    shown = show_procedure(QUESTION, db_path=db, sources_path=sources)
    assert shown.startswith("Kontrollquote bei E-Commerce-Sendungen aus dem Ausland")
    assert "Drucksache 21/5580, Frage 9" in shown  # the record's own body
    assert ("=== 2026-04-24 Schriftliche Frage/Schriftliche Antwort - Drucksache 21/5580 "
            "(Schriftliche Fragen), Frage 9 ===\n[not fetched yet]") in shown
    assert show_procedure("404", db_path=db, sources_path=sources) is None
