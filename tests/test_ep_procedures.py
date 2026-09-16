"""EP procedures: references, body, versioning, what is polled when, and selection."""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import ROOT
from src.db import get_watermark, migrate, session, start_run
from src.ep_procedures import (
    DORMANT_AFTER_DAYS, EpError, EpRateLimited, compose_body, content_hash, due_procedures,
    is_dormant, is_final, process_id, run_ep_collection, store_procedure,
)
from src.profile import load_profile
from src.selector import UNFILTERED_REASON, selection_from_db

RECORD = json.loads((Path(__file__).parent / "fixtures" / "ep_procedure_2023_0156.json")
                    .read_text(encoding="utf-8"))["data"][0]
SLUG = "oeil.secure.europarl.europa.eu"
REFERENCE = "2023/0156(COD)"
TODAY = date(2026, 9, 12)
EP_ENTRY = {"url": "https://oeil.secure.europarl.europa.eu/", "organization": "EP",
            "collector": "ep_procedures", "keyword_prefilter": False,
            "procedure_types": ["COD", "CNS", "APP"], "since_year": 2021,
            "content_mode": "full_text", "access": "free"}
LABELS = ROOT / "clients" / "jt-express" / "labels" / "ep_procedures_2026-09-11.json"


def record(**changes) -> dict:
    return dict(copy.deepcopy(RECORD), **changes)


def listed(*references) -> list[dict]:
    return [{"process_id": process_id(r), "label": r, "process_type": r[-4:-1]}
            for r in references]


@pytest.fixture()
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    sources = tmp_path / "regulatory.json"
    sources.write_text(json.dumps([EP_ENTRY]), encoding="utf-8")
    return db, sources, tmp_path


def rows(db):
    with session(db) as conn:
        return list(conn.execute("SELECT * FROM raw_item ORDER BY id"))


def test_reference_becomes_the_api_process_id():
    assert process_id("2023/0156(COD)") == "2023-0156"
    with pytest.raises(ValueError):
        # A CELEX number is exactly what must never be turned into an id.
        process_id("52023PC0258")


def test_body_shows_stage_agenda_and_events():
    body = compose_body(REFERENCE, record())
    assert body.startswith("European Parliament procedure 2023/0156(COD), "
                           "ordinary legislative procedure")
    assert "Title: Establishing the Union Customs Code" in body
    assert "Current stage: second reading (RDG2)" in body
    agenda = body.split("On the Parliament's agenda:")[1].split("Events:")[0]
    assert "2026-09-16 PLENARY_VOTE" in agenda
    assert "2026-04-16 COMMITTEE_APPROVE_PROVISIONAL_AGREEMENT" in body.split("Events:")[1]


def test_hash_ignores_list_order_but_sees_a_newly_scheduled_vote():
    base = content_hash(record())
    shuffled = record()
    shuffled["consists_of"].reverse()
    shuffled["was_scheduled_in"].reverse()
    assert content_hash(shuffled) == base
    scheduled = record()
    scheduled["was_scheduled_in"].append({
        "id": "eli/dl/event/MTG-PL-2026-10-06-OJ-ITM-V-1", "type": "ForeseenActivity",
        "activity_date": "2026-10-06", "had_activity_type": "def/ep-activities/PLENARY_VOTE"})
    assert content_hash(scheduled) != base


def test_store_is_idempotent_and_dates_by_the_last_event(project):
    db, _, _ = project
    changed = record()
    changed["current_stage"] = changed["current_stage"].replace("RDG2", "RDG3")
    with session(db) as conn:
        run = start_run(conn, "ep_procedures", "a", "b")
        assert store_procedure(conn, run, SLUG, REFERENCE, record()) == 1
        assert store_procedure(conn, run, SLUG, REFERENCE, record()) == 0
        assert store_procedure(conn, run, SLUG, REFERENCE, changed) == 1
    first, second = rows(db)
    assert (first["version"], second["version"]) == (1, 2)
    assert first["source_kind"] == "regulatory"
    assert first["published_at"] == "2026-09-04"
    assert first["title"].startswith("Einführung des Zollkodexes der Union")
    assert first["url"].endswith("procedure-file?reference=2023/0156(COD)")
    assert json.loads(first["payload"])["published_at_source"] == "record"


# ── what gets polled ──────────────────────────────────────────────────────

def published_law() -> dict:
    law = record()
    law["consists_of"] = law["consists_of"] + [{
        "id": "eli/dl/event/oj", "activity_date": "2026-06-01",
        "had_activity_type": "def/ep-activities/PUBLICATION_OFFICIAL_JOURNAL"}]
    return law


def old_law() -> dict:
    """Published in the Official Journal long before we ever looked."""
    done = record()
    done["consists_of"] = [
        {"id": "eli/dl/event/ref", "activity_date": "2024-01-10",
         "had_activity_type": "def/ep-activities/REFERRAL"},
        {"id": "eli/dl/event/oj", "activity_date": "2024-06-01",
         "had_activity_type": "def/ep-activities/PUBLICATION_OFFICIAL_JOURNAL"}]
    done["was_scheduled_in"] = []
    return done


def dormant() -> dict:
    old = record()
    old["consists_of"] = [{"id": "eli/dl/event/old", "activity_date": "2023-01-10",
                           "had_activity_type": "def/ep-activities/REFERRAL"}]
    old["was_scheduled_in"] = []
    return old


def test_dormant_is_about_events_and_the_agenda():
    assert is_dormant(dormant(), TODAY)
    assert not is_dormant(record(), TODAY)
    assert is_final(published_law()) and not is_final(record())


def test_new_and_active_are_due_every_run_dormant_only_on_a_sweep():
    stored = {"2023/0156(COD)": record(), "2019/0001(COD)": dormant(),
              "2022/0002(COD)": published_law()}
    every_run = due_procedures(
        listed("2023/0156(COD)", "2019/0001(COD)", "2022/0002(COD)", "2026/0009(COD)"),
        stored, TODAY, sweep=False)
    # The law is never polled again; the dormant one waits for the sweep.
    assert [reference for reference, _ in every_run] == ["2023/0156(COD)", "2026/0009(COD)"]
    swept = due_procedures(
        listed("2023/0156(COD)", "2019/0001(COD)", "2022/0002(COD)"), stored, TODAY, sweep=True)
    assert [reference for reference, _ in swept] == ["2023/0156(COD)", "2019/0001(COD)"]


def test_a_split_file_keeps_the_id_the_listing_gave_it():
    """2021/0211A(COD) is a real reference; no rule of ours would derive its id."""
    split = [{"process_id": "2021-0211A", "label": "2021/0211A(COD)", "process_type": "COD"}]
    assert due_procedures(split, {}, TODAY, sweep=False) == [("2021/0211A(COD)", "2021-0211A")]
    with pytest.raises(ValueError):
        process_id("2021/0211A(COD)")
    # A listing entry with no id at all is reported rather than guessed at.
    assert due_procedures([{"label": "2021/0211A(COD)"}], {}, TODAY, sweep=False) == [
        ("2021/0211A(COD)", None)]


class FakeClient:
    def __init__(self, records, *, years=None, refuse=()):
        self.records = records
        self.years = years or {2026: list(records)}
        self.refuse = set(refuse)
        self.calls: list[str] = []

    def list_procedures(self, year):
        return listed(*self.years.get(year, []))

    def procedure(self, pid):
        self.calls.append(pid)
        if pid in self.refuse:
            raise EpRateLimited(f"{pid}: giving up after 4 attempts - HTTP 429")
        return copy.deepcopy(self.records.get(pid))


def run(db, sources, client, **kwargs):
    return run_ep_collection(client=client, db_path=db, sources_path=sources,
                             since_year=2026, today=TODAY, **kwargs)


def test_first_run_stores_open_procedures_and_skips_law_it_never_saw(project):
    db, sources, _ = project
    client = FakeClient({"2023-0156": record(), "2022-0002": old_law(),
                         "2026-0009": None},
                        years={2026: ["2023/0156(COD)", "2022/0002(COD)", "2026/0009(COD)"]})
    s = run(db, sources, client)
    assert (s["listed"], s["due"], s["fetched"], s["stored"]) == (3, 3, 1, 1)
    # Law since 2024, first seen in September 2026: history, not this week's news.
    assert s["stale"] == 1
    # Listed by the API and then unknown to it: visible in the note, not a failure.
    assert s["unknown"] == ["2026/0009(COD)"] and s["errors"] == []
    assert s["swept"] is True
    with session(db) as conn:
        run_row = conn.execute("SELECT status, note FROM run").fetchone()
        assert conn.execute("SELECT status FROM run_source").fetchone()["status"] == "ok"
    assert run_row["status"] == "ok"
    assert "1 listed but unknown to the API: 2026/0009(COD)" in run_row["note"]

    stored = {r["external_id"]: json.loads(r["payload"]) for r in rows(db)}
    assert stored[REFERENCE]["body_text"]
    # The old law is remembered so it is not fetched again, but carries no body,
    # so no gate reads it.
    history = stored["2022/0002(COD)"]
    assert history["closed_before_first_sight"] is True and "body_text" not in history
    again = FakeClient({"2023-0156": record(), "2022-0002": old_law(), "2026-0009": None},
                       years={2026: ["2023/0156(COD)", "2022/0002(COD)", "2026/0009(COD)"]})
    second = run(db, sources, again)
    assert "2022-0002" not in again.calls and second["stale"] == 0
    with session(db) as conn:
        assert get_watermark(conn, f"collection:regulatory:{SLUG}:sweep") == TODAY.isoformat()


def test_a_procedure_that_becomes_law_is_stored_once_more_then_left_alone(project):
    db, sources, _ = project
    years = {2026: [REFERENCE]}
    run(db, sources, FakeClient({"2023-0156": record()}, years=years))
    after = FakeClient({"2023-0156": published_law()}, years=years)
    s = run(db, sources, after)
    assert s["stored"] == 1 and s["final"] == [REFERENCE]
    done = FakeClient({"2023-0156": published_law()}, years=years)
    s = run(db, sources, done)
    assert done.calls == [] and s["due"] == 0


def test_repeated_rate_limiting_stops_the_run_and_holds_the_sweep(project):
    db, sources, _ = project
    references = ["2026/0001(COD)", "2026/0002(COD)", "2026/0003(COD)", "2026/0004(COD)"]
    client = FakeClient({}, years={2026: references},
                        refuse={process_id(r) for r in references})
    s = run(db, sources, client)
    assert s["stopped"] and "rate limited" in s["stopped"]
    # Stopped at the third refusal rather than working through the whole list.
    assert len(client.calls) == 3
    assert s["fetched"] == 0 and s["swept"] is False
    # Throttling is a failure the health observer must see, not a quiet day.
    assert len(s["errors"]) == 3 and all("HTTP 429" in e for e in s["errors"])
    with session(db) as conn:
        assert get_watermark(conn, f"collection:regulatory:{SLUG}:sweep") is None
        assert conn.execute("SELECT status FROM run").fetchone()["status"] == "failed"
        assert conn.execute("SELECT status FROM run_source").fetchone()["status"] == "failed"


def test_a_throttled_dormant_procedure_is_swept_again_the_next_day(project):
    db, sources, _ = project
    dormant_ref = "2019/0001(COD)"
    years = {2026: [dormant_ref, REFERENCE]}
    records = {"2019-0001": dormant(), "2023-0156": record()}

    def collect(day, client):
        return run_ep_collection(client=client, db_path=db, sources_path=sources,
                                 since_year=2026, today=day)

    collect(TODAY, FakeClient(records, years=years))
    sweep_day = TODAY + timedelta(days=7)
    # One isolated refusal: the run carries on, but the dormant file was not read.
    s = collect(sweep_day, FakeClient(records, years=years, refuse={"2019-0001"}))
    assert s["fetched"] == 1 and s["stopped"] is None
    assert len(s["errors"]) == 1 and s["errors"][0].startswith(dormant_ref)
    assert s["swept"] is False
    with session(db) as conn:
        assert get_watermark(conn, f"collection:regulatory:{SLUG}:sweep") == TODAY.isoformat()
        assert conn.execute("SELECT status FROM run ORDER BY id DESC").fetchone()["status"] == "failed"

    retry = FakeClient(records, years=years)
    s = collect(sweep_day + timedelta(days=1), retry)
    assert "2019-0001" in retry.calls and s["swept"] is True and s["errors"] == []


def test_a_failed_listing_collects_nothing_and_says_so(project):
    db, sources, _ = project

    class Broken(FakeClient):
        def list_procedures(self, year):
            raise EpRateLimited("procedures: giving up after 4 attempts - HTTP 429")

    s = run(db, sources, Broken({}))
    assert s["fetched"] == 0 and s["errors"] and s["errors"][0].startswith("listing:")
    assert s["listing_failed"] is True
    with session(db) as conn:
        assert conn.execute("SELECT status FROM run_source").fetchone()["status"] == "failed"


def test_a_server_error_on_a_known_procedure_still_fails_the_run(project):
    db, sources, _ = project

    class ServerError(FakeClient):
        def procedure(self, pid):
            if pid == "2026-0001":
                raise EpError(f"{pid}: giving up after 4 attempts - HTTP 500")
            return super().procedure(pid)

    client = ServerError({"2023-0156": record()},
                         years={2026: [REFERENCE, "2026/0001(COD)"]})
    s = run(db, sources, client)
    assert s["fetched"] == 1 and s["unknown"] == [] and s["listing_failed"] is False
    assert "HTTP 500" in s["errors"][0]
    with session(db) as conn:
        assert conn.execute("SELECT status FROM run").fetchone()["status"] == "failed"


# ── client side ───────────────────────────────────────────────────────────

def test_a_procedure_reaches_the_gate_without_a_keyword(project):
    db, sources, tmp_path = project
    vehicles = record()
    vehicles["process_title"] = {"de": "Saubere Unternehmensfahrzeuge",
                                 "en": "Clean corporate vehicles"}
    vehicles["consists_of"] = vehicles["consists_of"][:1]
    vehicles["created_a_realization_of"], vehicles["was_scheduled_in"] = [], []
    with session(db) as conn:
        store_procedure(conn, start_run(conn, "ep_procedures", "a", "b"), SLUG,
                        "2025/0421(COD)", vehicles)
    profile = load_profile("jt-express")
    (candidate,) = selection_from_db(db, ((sources, "regulatory"),), "all",
                                     profile).candidates
    assert candidate.reasons == (UNFILTERED_REASON,) and candidate.has_body

    prefiltered = tmp_path / "prefiltered.json"
    prefiltered.write_text(json.dumps([dict(EP_ENTRY, keyword_prefilter=True)]),
                           encoding="utf-8")
    assert selection_from_db(db, ((prefiltered, "regulatory"),), "all",
                             profile).candidates == ()


def test_the_client_expectation_set_is_readable_and_decides_nothing():
    """The 14 hand-picked procedures are a recall check, not collection config."""
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    procedures = labels["procedures"]
    assert len(procedures) == 14
    for entry in procedures:
        process_id(entry["reference"])          # every reference is well formed
        assert entry["tier"] in {"core", "moderate", "marginal"}
        assert entry["expected"] in {"relevant", "unsure"}
        assert entry["why"]
    must_keep = [e["reference"] for e in procedures if e["expected"] == "relevant"]
    assert REFERENCE in must_keep and "2023/0158(CNS)" in must_keep
    assert not (ROOT / "input" / "eu_procedures.json").exists()
