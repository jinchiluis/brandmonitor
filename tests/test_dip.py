"""DIP procedures: body, versioning, first-sight rule, watermark, and pipeline fit."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.bodies import run_body_fetch
from src.collect import crawled_entries, source_watermark_scope
from src.db import get_watermark, migrate, session, start_run
from src.dip import (
    SOURCE_KIND, DipError, DipKeyError, compose_body, content_hash, run_dip_collection,
    store_procedure, web_url,
)
from src.profile import load_profile
from src.selector import selection_from_db

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dip_procedures.json")
                     .read_text(encoding="utf-8"))
SLUG = "dip.bundestag.de"
NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
DIP_ENTRY = {"url": "https://dip.bundestag.de/", "organization": "DIP", "collector": "dip",
             "excluded_vorgangstypen": ["Wahl im BT"], "content_mode": "full_text",
             "access": "free"}


def procedure(pid: str) -> dict:
    return copy.deepcopy(next(p for p in FIXTURE["vorgang"] if p["id"] == pid))


def steps(pid: str) -> list[dict]:
    return copy.deepcopy([s for s in FIXTURE["vorgangsposition"] if s["vorgang_id"] == pid])


QUESTION, MINOR_QUESTION, RESOLUTION = "334627", "337163", "322138"


@pytest.fixture()
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    sources = tmp_path / "regulatory.json"
    sources.write_text(json.dumps([
        {"url": "https://regulator.test/", "organization": "Regulator",
         "content_mode": "full_text", "access": "free"},
        DIP_ENTRY,
    ]), encoding="utf-8")
    return db, sources


def rows(db):
    with session(db) as conn:
        return list(conn.execute("SELECT * FROM raw_item ORDER BY id"))


# ── the record as text ────────────────────────────────────────────────────

def test_body_names_type_state_descriptors_abstract_and_every_step():
    body = compose_body(procedure(RESOLUTION), steps(RESOLUTION))
    assert body.startswith("Selbständiger Antrag von Ländern auf Entschließung, 21. Wahlperiode")
    assert "Stand: Angenommen" in body
    assert "Initiative: Baden-Württemberg, Mecklenburg-Vorpommern" in body
    assert "Deskriptoren: Drittstaat, Einfuhrbeschränkung, Elektronischer Handel" in body
    # DIP abstracts carry HTML entities; the body must not.
    assert 'Aufhebung der Zollfreigrenze, "Retouren-Steuer"' in body
    assert "&quot;" not in body and "<br" not in body
    # The record's date is the government's reply; the adoption is a year earlier,
    # and the body is where a reader can tell the two apart.
    assert ("2025-07-11 BR-Sitzung (BR) - Plenarprotokoll 1056 - Annahme in geänderter "
            "Fassung") in body
    assert "2026-07-16 Unterrichtung (BR) - Drucksache zu228/25(B) - Stellungnahme der " \
           "Bundesregierung" in body


def test_written_question_step_names_the_question_and_who_answered():
    body = compose_body(procedure(QUESTION), steps(QUESTION))
    assert "Drucksache 21/5580, Frage 9" in body
    assert "Antwort: Michael Schrodi, Parl. Staatssekr., Bundesministerium der Finanzen" in body


def test_web_url_ends_in_the_procedure_id():
    url = web_url(procedure(QUESTION))
    assert url == ("https://dip.bundestag.de/vorgang/"
                   "kontrollquote-bei-e-commerce-sendungen-aus-dem-ausland/334627")


# ── versions ──────────────────────────────────────────────────────────────

def test_hash_ignores_documentation_stamps_but_not_content():
    record, course = procedure(RESOLUTION), steps(RESOLUTION)
    base = content_hash(record, course)
    touched = dict(record, aktualisiert="2027-01-01T00:00:00+01:00")
    touched_steps = [dict(s, aktualisiert="2027-01-01T00:00:00+01:00") for s in course]
    assert content_hash(touched, list(reversed(touched_steps))) == base
    assert content_hash(dict(record, beratungsstand="Abgelehnt"), course) != base


def test_store_is_idempotent_and_a_new_step_appends_a_version(project):
    db, _ = project
    record, course = procedure(MINOR_QUESTION), steps(MINOR_QUESTION)
    with session(db) as conn:
        run = start_run(conn, "dip", "a", "b")
        assert store_procedure(conn, run, SLUG, record, course[:1]) == 1
        assert store_procedure(conn, run, SLUG, record, course[:1]) == 0
        restamped = dict(record, aktualisiert="2027-05-01T00:00:00+02:00")
        assert store_procedure(conn, run, SLUG, restamped, course[:1]) == 0
        # The government's answer arrives: that is the news, so it is a version.
        assert store_procedure(conn, run, SLUG, record, course) == 1
    stored = rows(db)
    assert [r["version"] for r in stored] == [1, 2]
    latest = stored[-1]
    assert latest["source_kind"] == SOURCE_KIND == "regulatory"
    assert latest["external_id"] == MINOR_QUESTION
    assert latest["published_at"] == "2026-07-27"
    assert latest["title"].startswith("Auswirkungen der EU-Zollreform")
    payload = json.loads(latest["payload"])
    assert payload["published_at_source"] == "record"
    assert [p["vorgangsposition"] for p in payload["positionen"]] == ["Kleine Anfrage", "Antwort"]
    assert "Antwort (BT) - Drucksache 21/7373" in payload["body_text"]


# ── collection runs ───────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, procedures, *, fail_steps=False, key_error=False):
        self.pages = [procedures]
        self.fail_steps = fail_steps
        self.key_error = key_error
        self.since: list[str] = []
        self.step_calls: list[list[str]] = []

    def procedures(self, since):
        self.since.append(since)
        if self.key_error:
            raise DipKeyError("HTTP 401 - DIP rejected the API key")
        yield from self.pages

    def positions(self, ids):
        self.step_calls.append(list(ids))
        if self.fail_steps:
            raise DipError("vorgangsposition: giving up after 4 attempts")
        return [s for s in FIXTURE["vorgangsposition"] if s["vorgang_id"] in ids]


def listing():
    procedural = dict(procedure(QUESTION), id="999", vorgangstyp="Wahl im BT",
                      datum="2026-09-10")
    # The written question's latest step is 2026-04-24: first seen in a run from
    # 2026-08-11 it is the re-indexed archive, not news.
    return [procedure(QUESTION), procedure(MINOR_QUESTION), procedure(RESOLUTION), procedural]


def test_first_run_skips_procedural_types_and_the_reindexed_archive(project):
    db, sources = project
    client = FakeClient(listing())
    s = run_dip_collection(client=client, db_path=db, sources_path=sources, now=NOW)
    assert (s["listed"], s["excluded"], s["stale"], s["found"], s["stored"]) == (4, 1, 1, 2, 2)
    assert client.since == ["2026-08-11T00:00:00"]
    assert client.step_calls == [[MINOR_QUESTION, RESOLUTION]]
    assert {r["external_id"] for r in rows(db)} == {MINOR_QUESTION, RESOLUTION}
    with session(db) as conn:
        outcome = conn.execute("SELECT status, items_found, items_stored FROM run_source "
                               "WHERE source_slug = ?", (SLUG,)).fetchone()
    assert tuple(outcome) == ("ok", 2, 2)


def test_a_known_procedure_takes_changes_however_old_its_step(project):
    db, sources = project
    with session(db) as conn:
        run = start_run(conn, "dip", "a", "b")
        store_procedure(conn, run, SLUG, procedure(QUESTION), [])
    s = run_dip_collection(client=FakeClient([procedure(QUESTION)]), db_path=db,
                           sources_path=sources, now=NOW)
    assert (s["stale"], s["found"], s["stored"]) == (0, 1, 1)
    assert [r["version"] for r in rows(db)] == [1, 2]


def test_watermark_is_the_run_start_and_the_next_run_overlaps_a_day(project):
    db, sources = project
    run_dip_collection(client=FakeClient(listing()), db_path=db, sources_path=sources, now=NOW)
    with session(db) as conn:
        assert get_watermark(conn, source_watermark_scope("regulatory", SLUG)) == NOW.isoformat()

    later = FakeClient(listing())
    s = run_dip_collection(client=later, db_path=db, sources_path=sources,
                           now=NOW + timedelta(days=1))
    assert later.since == ["2026-09-10T00:00:00"]
    assert s["stored"] == 0


def test_failed_steps_store_nothing_and_hold_the_watermark(project):
    db, sources = project
    s = run_dip_collection(client=FakeClient(listing(), fail_steps=True), db_path=db,
                           sources_path=sources, now=NOW)
    assert s["failed_batches"] == 1 and s["stored"] == 0 and s["watermark_advanced"] is None
    assert rows(db) == []
    with session(db) as conn:
        assert get_watermark(conn, source_watermark_scope("regulatory", SLUG)) is None


def test_rejected_key_closes_the_run_as_failed_and_is_raised(project):
    db, sources = project
    with pytest.raises(DipKeyError):
        run_dip_collection(client=FakeClient([], key_error=True), db_path=db,
                           sources_path=sources, now=NOW)
    with session(db) as conn:
        run = conn.execute("SELECT status FROM run WHERE kind = 'dip'").fetchone()
        source = conn.execute("SELECT status FROM run_source WHERE source_slug = ?",
                              (SLUG,)).fetchone()
    assert run["status"] == "failed" and source["status"] == "failed"


# ── fit with the rest of the pipeline ─────────────────────────────────────

def test_collector_entries_are_neither_crawled_nor_bulk_fetched(project):
    db, sources = project
    entries = json.loads(sources.read_text(encoding="utf-8"))
    assert [e["url"] for e in crawled_entries(entries)] == ["https://regulator.test/"]
    with session(db) as conn:
        store_procedure(conn, start_run(conn, "dip", "a", "b"), SLUG,
                        procedure(RESOLUTION), steps(RESOLUTION))
    summary = run_body_fetch(sources, kind="regulatory", db_path=db)
    assert summary["attempted"] == 0
    with session(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM body_fetch").fetchone()[0] == 0


def test_selector_reads_a_procedure_as_a_regulatory_body(project):
    db, sources = project
    with session(db) as conn:
        store_procedure(conn, start_run(conn, "dip", "a", "b"), SLUG,
                        procedure(RESOLUTION), steps(RESOLUTION))
    result = selection_from_db(db, ((sources, "regulatory"),), "all",
                               load_profile("jt-express"))
    (candidate,) = result.candidates
    assert candidate.source_slug == SLUG and candidate.has_body
    assert candidate.title.startswith("Entschließung des Bundesrates")
    # "Drittstaaten" in the title; the Zollfreigrenze only in the abstract.
    assert "keyword:third country" in candidate.reasons
    assert "topic:Zollfreigrenze" in candidate.reasons
