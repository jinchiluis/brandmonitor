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
    client_match_reasons, parse_report_detail, parse_report_list,
    run_safety_gate_collection, store_alert,
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
