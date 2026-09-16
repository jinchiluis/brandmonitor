"""EU Safety Gate parsing, versioning, resumption, and client filtering."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from src.db import get_watermark, migrate, session, set_watermark, start_run
from src.profile import load_profile
from src.safety_gate import (
    SOURCE_KIND, WATERMARK_SCOPE, SafetyGateError, WeeklyReport,
    client_match_reasons, compose_body, parse_measures, parse_report_detail,
    parse_report_list, run_safety_gate_collection, select_client_alerts,
    store_alert,
)

FIXTURES = Path(__file__).parent / "fixtures"
LIST_XML = (FIXTURES / "safety_gate_report_list.xml").read_bytes()
DETAIL_XML = (FIXTURES / "safety_gate_report_2026_36.xml").read_bytes()


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.sqlite3"
    migrate(path)
    return path


def reports():
    return parse_report_list(LIST_XML)


def alerts():
    return parse_report_detail(DETAIL_XML, reports()[-1])


def test_report_list_is_validated_and_sorted_oldest_first():
    parsed = reports()
    assert [r.report_id for r in parsed] == [10000321, 10000322]
    assert parsed[-1].publication_date == "2026-09-11"


def test_detail_preserves_native_fields_nil_values_pictures_and_provenance():
    first, second = alerts()
    assert first["caseNumber"] == "SR/02420/26"
    assert first["brand"] is None
    assert first["barcode"] == "20241210"
    assert first["pictures"] == [
        "https://ec.europa.eu/image/1", "https://ec.europa.eu/image/2"]
    assert first["notificationType"].endswith("MotorVehicleContentDto")
    assert first["report"]["report_id"] == 10000322
    assert second["pictures"] == ["https://ec.europa.eu/image/3"]


def test_detail_date_must_match_the_list():
    wrong = replace(reports()[-1], publication_date="2026-09-10")
    with pytest.raises(SafetyGateError, match="does not match"):
        parse_report_detail(DETAIL_XML, wrong)


def test_an_error_document_is_not_an_empty_report():
    # Well-formed XML with no notifications, which used to read as a quiet week.
    report = reports()[-1]
    with pytest.raises(SafetyGateError, match="expected a Safety-Gate report"):
        parse_report_detail(b"<error>temporarily unavailable</error>", report)
    with pytest.raises(SafetyGateError, match="no report_date"):
        parse_report_detail(
            b"<Safety-Gate><report_year>2026</report_year></Safety-Gate>", report)


def test_a_week_without_notifications_is_still_a_report():
    empty = (b"<Safety-Gate><report_language>en</report_language>"
             b"<report_date>11/09/2026</report_date></Safety-Gate>")
    assert parse_report_detail(empty, reports()[-1]) == []


def test_store_is_idempotent_and_changed_alert_appends_a_version(db):
    payload = alerts()[0]
    with session(db) as conn:
        run_id = start_run(conn, SOURCE_KIND, "2026-09-11", "2026-09-11")
        assert store_alert(conn, run_id, payload) == 1
        assert store_alert(conn, run_id, payload) == 0
        changed = dict(payload, danger="Updated official danger text")
        assert store_alert(conn, run_id, changed) == 1
        rows = list(conn.execute(
            "SELECT version, source_kind, published_at, payload FROM raw_item "
            "ORDER BY version"))
    assert [row["version"] for row in rows] == [1, 2]
    assert {row["source_kind"] for row in rows} == {SOURCE_KIND}
    assert rows[0]["published_at"] == "2026-09-11"
    assert json.loads(rows[1]["payload"])["danger"].startswith("Updated")


class FakeClient:
    def __init__(self, *, fail=()):
        self.fail = set(fail)
        self.calls: list[int] = []

    def list_reports(self):
        return reports()

    def get_report(self, report):
        self.calls.append(report.report_id)
        if report.report_id in self.fail:
            raise SafetyGateError("temporary detail failure")
        # Give each report distinct case ids while retaining the real fixture shape.
        result = alerts()
        for index, payload in enumerate(result):
            payload["caseNumber"] = f"{report.report_id}-{index}"
            payload["report"] = {
                "report_id": report.report_id,
                "reference": report.reference,
                "publication_date": report.publication_date,
                "url": report.url,
                "language": report.language,
                "year": report.year,
                "week": report.week,
            }
        return result


class ErrorDocumentClient(FakeClient):
    """Answers the ids in ``fail`` with a well-formed error document."""

    def get_report(self, report):
        if report.report_id not in self.fail:
            return super().get_report(report)
        self.calls.append(report.report_id)
        return parse_report_detail(b"<error>temporarily unavailable</error>", report)


def runs(db):
    with session(db) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT r.id, r.status, r.note, s.status AS source_status, s.error "
            "FROM run r JOIN run_source s ON s.run_id = r.id ORDER BY r.id")]


def test_first_run_backfills_then_the_watermark_makes_next_run_free(db):
    first = FakeClient()
    summary = run_safety_gate_collection(
        lookback_weeks=2, client=first, db_path=db)
    assert summary["reports_ok"] == 2
    assert summary["found"] == 4
    assert summary["stored"] == 4
    assert summary["watermark_advanced"] == 10000322

    again = FakeClient()
    summary = run_safety_gate_collection(client=again, db_path=db)
    assert summary["note"] == "up to date"
    assert again.calls == []
    # The quiet check is still a run, so health can tell it from one that never ran.
    check = runs(db)[-1]
    assert check["id"] == summary["run_id"]
    assert (check["status"], check["source_status"]) == ("ok", "zero")
    assert check["note"] == "up to date: official reports complete through 2026-09-11"


def test_a_check_that_cannot_read_the_list_is_a_failed_run(db):
    class Unreachable(FakeClient):
        def list_reports(self):
            raise SafetyGateError("weekly report list: giving up after 4 attempts")

    summary = run_safety_gate_collection(client=Unreachable(), db_path=db)
    assert summary["aborted"].startswith("weekly report list")
    [check] = runs(db)
    assert check["id"] == summary["run_id"]
    assert (check["status"], check["source_status"]) == ("failed", "failed")
    assert "giving up after 4 attempts" in check["error"]
    with session(db) as conn:
        assert get_watermark(conn, WATERMARK_SCOPE) is None


def test_an_error_document_holds_the_watermark_and_is_retried(db):
    broken = ErrorDocumentClient(fail={10000321, 10000322})
    summary = run_safety_gate_collection(lookback_weeks=2, client=broken, db_path=db)
    assert (summary["reports_ok"], summary["reports_failed"]) == (0, 2)
    assert summary["watermark_advanced"] is None
    assert runs(db)[-1]["status"] == "failed"
    with session(db) as conn:
        assert get_watermark(conn, WATERMARK_SCOPE) is None

    healed = FakeClient()
    summary = run_safety_gate_collection(lookback_weeks=2, client=healed, db_path=db)
    assert healed.calls == [10000321, 10000322]
    assert summary["stored"] == 4
    assert summary["watermark_advanced"] == 10000322


def test_failed_report_stops_the_watermark_and_is_retried(db):
    broken = FakeClient(fail={10000322})
    summary = run_safety_gate_collection(
        lookback_weeks=2, client=broken, db_path=db)
    assert summary["reports_failed"] == 1
    assert summary["watermark_advanced"] == 10000321
    with session(db) as conn:
        assert get_watermark(conn, WATERMARK_SCOPE) == "10000321"

    healed = FakeClient()
    summary = run_safety_gate_collection(client=healed, db_path=db)
    assert healed.calls == [10000322]
    assert summary["watermark_advanced"] == 10000322


def test_explicit_disconnected_window_does_not_jump_a_watermark(db):
    with session(db) as conn:
        set_watermark(conn, WATERMARK_SCOPE, "10000321")
    client = FakeClient()
    summary = run_safety_gate_collection(
        weeks=1, end=date(2026, 9, 4), client=client, db_path=db)
    assert summary["watermark_advanced"] is None
    with session(db) as conn:
        assert get_watermark(conn, WATERMARK_SCOPE) == "10000321"


def test_pilot_view_requires_germany_and_china_then_matches_online_trader():
    profile = load_profile("jt-express")
    payload = alerts()[0]
    assert client_match_reasons(payload, profile) == (
        "notifying_country:Germany", "origin:China", "online_trader:Shein")
    assert client_match_reasons(dict(payload, notifyingCountry="Ireland"), profile) == ()
    assert client_match_reasons(dict(payload, countryOfOrigin="Italy"), profile) == ()


def test_measures_split_into_one_dict_each_with_iso_dates():
    # The field arrives run together: label, value, next label, no separators.
    text = ("Type of economic operator taking notified measure(s): Manufacturer"
            "Category of measure(s): Recall of the product from end users"
            "Date of entry into force: 22/05/2026"
            "Type of economic operator taking notified measure(s): Manufacturer"
            "Category of measure(s): Withdrawal of the product from the market"
            "Date of entry into force: 25/06/2026")
    assert parse_measures(text) == [
        {"operator": "Manufacturer",
         "category": "Recall of the product from end users",
         "in_force": "2026-05-22"},
        {"operator": "Manufacturer",
         "category": "Withdrawal of the product from the market",
         "in_force": "2026-06-25"},
    ]


def test_measures_tolerate_an_unlabelled_string_and_an_empty_one():
    # The fixture export states a measure with no labels at all.
    assert parse_measures("Removal of this product listing by the online marketplace") == [
        {"operator": None,
         "category": "Removal of this product listing by the online marketplace",
         "in_force": None},
    ]
    assert parse_measures(None) == []
    assert parse_measures("   ") == []


def test_composed_body_carries_the_fields_an_assessor_needs():
    body = compose_body(alerts()[0])
    assert body.startswith(
        "EU Safety Gate alert SR/02420/26, Serious risk, published 2026-09-11")
    for expected in ("Product: Necklace", "Risk: Chemical", "Notified by: Germany",
                     "Country of origin: People's Republic of China",
                     "Sold online through: Other(Shein (sj23050389953853544))",
                     "The rate of nickel release is too high.",
                     "Removal of this product listing by the online marketplace"):
        assert expected in body, expected
    # A nil field is omitted rather than printed as "None".
    assert "Brand:" not in body and "None" not in body


def test_composed_body_resolves_html_entities():
    # Stored payloads carry "Y&amp;H" - a customer must not read the escape.
    body = compose_body(dict(alerts()[0], brand="Y&amp;H"))
    assert "Brand: Y&H" in body


def test_client_view_composes_a_body_and_names_the_matched_customers(db):
    with session(db) as conn:
        run_id = start_run(conn, SOURCE_KIND, "2026-09-11", "2026-09-11")
        for payload in alerts():
            store_alert(conn, run_id, payload)

    view = select_client_alerts("jt-express", db_path=db)
    # Only the German Chinese-origin alert; the Greek/Italian one is excluded.
    assert [a["case_number"] for a in view] == ["SR/02420/26"]
    alert = view[0]
    assert alert["key_customers"] == ("Shein",)
    assert alert["measures"][0]["category"] == (
        "Removal of this product listing by the online marketplace")
    assert alert["body_text"] == compose_body(alert["payload"])


def test_client_view_stays_out_of_the_selector_and_the_gates(db):
    """Safety Gate relevance is two fields, so it must not reach a gate."""
    from src.selector import selection_from_db

    with session(db) as conn:
        run_id = start_run(conn, SOURCE_KIND, "2026-09-11", "2026-09-11")
        for payload in alerts():
            store_alert(conn, run_id, payload)

    result = selection_from_db(
        db, ((Path("input/germany_medias.json"), "news"),
             (Path("input/regulatory_sources.json"), "regulatory")),
        "all", load_profile("jt-express"))
    assert not [c for c in result.candidates if c.source_kind == SOURCE_KIND]
    assert result.eligible == 0


def test_measures_read_the_second_operator_spelling():
    # Both spellings are live in the stored corpus; only one was handled at first.
    parsed = parse_measures(
        "Type of economic operator to whom the measure(s) were ordered: Distributor"
        "Category of measure(s): Removal of this product listing by the online marketplace"
        "Date of entry into force: 14/08/2026")
    assert parsed == [{"operator": "Distributor",
                       "category": "Removal of this product listing by the online marketplace",
                       "in_force": "2026-08-14"}]


def test_an_undatable_measure_has_no_date_rather_than_the_word_unknown():
    parsed = parse_measures(
        "Type of economic operator taking notified measure(s): Other"
        "Category of measure(s): Stop of sales"
        "Date of entry into force: Unknown")
    assert parsed == [{"operator": "Other", "category": "Stop of sales",
                       "in_force": None}]
    body = compose_body(dict(alerts()[0], measures=(
        "Category of measure(s): Stop of salesDate of entry into force: Unknown")))
    assert "Stop of sales" in body
    assert "Unknown" not in body and "in force" not in body
