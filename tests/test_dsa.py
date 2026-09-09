"""DSA aggregate collection: payload shape, versioning, watermark, retries.

No test here touches the network. The API is represented by a fake client whose
responses mirror shapes actually observed against the live endpoint, including
the ones that surprised us: a transient 422, a batch filer's empty day, and the
fact that territorial_scope cannot be answered from SQL.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import get_watermark, migrate, session, start_run  # noqa: E402
from src.dsa import (  # noqa: E402
    ARTICLE_16, DsaError, build_day_payload, last_complete_day, load_platforms,
    run_dsa_collection, slug_for, store_day,
)

TEMU = {"platform_id": 82, "slug": "dsa-temu", "name": "Temu"}
SHEIN = {"platform_id": 78, "slug": "dsa-shein", "name": "Shein"}


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.sqlite3"
    migrate(path)
    return path


@pytest.fixture()
def run_id(db):
    """raw_item.first_run_id is a real foreign key, so a run must exist."""
    with session(db) as conn:
        return start_run(conn, "dsa", "2026-09-08", "2026-09-08")


@pytest.fixture()
def platforms_file(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps([
        TEMU, SHEIN,
        {"platform_id": 28, "slug": "dsa-amazon", "name": "Amazon Store",
         "enabled": False},
    ]), encoding="utf-8")
    return path


class FakeClient:
    """Stands in for DsaClient, recording every call so cost is testable."""

    def __init__(self, rows_by_day=None, counts=None, fail_days=(), spreads=None):
        self.rows_by_day = rows_by_day or {}
        self.counts = counts or {}
        self.fail_days = set(fail_days)
        self.spreads = spreads or {}
        self.sql_calls: list[str] = []
        self.count_calls: list[str] = []

    def sql(self, query):
        self.sql_calls.append(query)
        for day in self.fail_days:
            if f"'{day}'" in query:
                raise DsaError("/sql: HTTP 422: No alive nodes")
        # The collector makes two differently-shaped SQL calls per day.
        if "application_date" in query:
            for day, rows in self.spreads.items():
                if f"'{day}'" in query:
                    return rows
            return []
        for day, rows in self.rows_by_day.items():
            if f"'{day}'" in query:
                return rows
        return []

    def count(self, query_string):
        self.count_calls.append(query_string)
        return self.counts.get(query_string, 0)


# ── payload shaping ───────────────────────────────────────────────────────

def test_payload_sums_categories_and_isolates_the_platform():
    rows = [
        (82, "STATEMENT_CATEGORY_UNSAFE_AND_PROHIBITED_PRODUCTS", "SOURCE_VOLUNTARY", 100),
        (82, "STATEMENT_CATEGORY_UNSAFE_AND_PROHIBITED_PRODUCTS", ARTICLE_16, 5),
        (82, "STATEMENT_CATEGORY_SCAMS_AND_FRAUD", "SOURCE_VOLUNTARY", 7),
        (78, "STATEMENT_CATEGORY_SCAMS_AND_FRAUD", "SOURCE_VOLUNTARY", 999),
    ]
    p = build_day_payload(rows, TEMU, "2026-09-08", de_total=80, de_article_16=4)

    assert p["total"] == 112, "other platforms' rows must not leak in"
    assert p["by_category"]["STATEMENT_CATEGORY_UNSAFE_AND_PROHIBITED_PRODUCTS"] == 105
    assert p["by_source_type"][ARTICLE_16] == 5
    assert p["article_16"] == {"all_territories": 5, "territorial_scope_de": 4}
    assert p["territorial_scope_de"] == 80
    assert "European Commission-DG CONNECT" in p["attribution"]


def test_empty_day_is_a_real_zero_not_a_gap():
    """A batch filer's quiet day must round-trip as an explicit zero."""
    p = build_day_payload([], SHEIN, "2026-09-08", de_total=0, de_article_16=0)
    assert p["total"] == 0
    assert p["by_category"] == {}


def test_category_order_does_not_change_the_payload():
    """The hash is taken over the payload, so key order must be stable."""
    a = [(82, "B_CAT", "SOURCE_VOLUNTARY", 1), (82, "A_CAT", "SOURCE_VOLUNTARY", 2)]
    b = list(reversed(a))
    assert (json.dumps(build_day_payload(a, TEMU, "2026-09-08", 0, 0))
            == json.dumps(build_day_payload(b, TEMU, "2026-09-08", 0, 0)))


# ── storage and versioning ────────────────────────────────────────────────

def test_restoring_the_same_day_writes_nothing(db, run_id):
    p = build_day_payload([(82, "C", "SOURCE_VOLUNTARY", 5)], TEMU, "2026-09-08", 3, 0)
    with session(db) as conn:
        assert store_day(conn, run_id, "dsa-temu", p) == 1
        assert store_day(conn, run_id, "dsa-temu", p) == 0


def test_changed_counts_append_a_version_and_keep_the_first(db, run_id):
    day = "2026-09-08"
    first = build_day_payload([(82, "C", "SOURCE_VOLUNTARY", 5)], TEMU, day, 3, 0)
    later = build_day_payload([(82, "C", "SOURCE_VOLUNTARY", 9)], TEMU, day, 3, 0)
    with session(db) as conn:
        store_day(conn, run_id, "dsa-temu", first)
        assert store_day(conn, run_id, "dsa-temu", later) == 1
        rows = list(conn.execute(
            "SELECT version, payload FROM raw_item WHERE external_id = ? "
            "ORDER BY version", (day,)))
    assert [r["version"] for r in rows] == [1, 2]
    assert json.loads(rows[0]["payload"])["total"] == 5, "history must survive"


def test_attribution_alone_does_not_create_a_version(db, run_id):
    """Only counts are hashed, so boilerplate changes must not churn versions."""
    p = build_day_payload([(82, "C", "SOURCE_VOLUNTARY", 5)], TEMU, "2026-09-08", 3, 0)
    with session(db) as conn:
        store_day(conn, run_id, "dsa-temu", p)
        p2 = dict(p, attribution="changed", api={"base": "elsewhere"})
        assert store_day(conn, run_id, "dsa-temu", p2) == 0


# ── configuration ─────────────────────────────────────────────────────────

def test_disabled_platforms_are_skipped(platforms_file):
    names = [p["name"] for p in load_platforms(platforms_file)]
    assert names == ["Temu", "Shein"]


def test_slug_falls_back_to_the_id():
    assert slug_for({"platform_id": 99, "name": "X"}) == "dsa-99"


def test_platform_entry_must_carry_an_id(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "Temu"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="platform_id"):
        load_platforms(bad)


def test_today_is_never_collected():
    """Today is still being filed into, so its count would be wrong."""
    from datetime import datetime, timezone
    now = datetime(2026, 9, 9, 23, 59, tzinfo=timezone.utc)
    assert last_complete_day(now) == date(2026, 9, 8)


# ── run orchestration ─────────────────────────────────────────────────────

END = date(2026, 9, 8)


def _rows(pid, n):
    return [(pid, "STATEMENT_CATEGORY_SCAMS_AND_FRAUD", "SOURCE_VOLUNTARY", n)]


def test_run_stores_one_row_per_platform_day(db, platforms_file):
    client = FakeClient(rows_by_day={
        "2026-09-07": _rows(82, 10) + _rows(78, 2),
        "2026-09-08": _rows(82, 20) + _rows(78, 4),
    })
    s = run_dsa_collection(platforms_file, days=2, client=client, end=END, db_path=db)
    assert s["failed"] == 0
    assert s["stored"] == 4, "2 platforms x 2 days"
    with session(db) as conn:
        kinds = {r["source_kind"] for r in conn.execute(
            "SELECT source_kind FROM raw_item")}
    assert kinds == {"dsa"}


def test_rerunning_a_complete_window_stores_nothing(db, platforms_file):
    client = FakeClient(rows_by_day={"2026-09-08": _rows(82, 10)})
    run_dsa_collection(platforms_file, days=1, client=client, end=END, db_path=db)
    again = run_dsa_collection(platforms_file, days=1, client=client, end=END, db_path=db)
    assert again["stored"] == 0


def test_per_day_api_cost_is_two_sql_calls_plus_two_counts_per_platform(db, platforms_file):
    """The DE figures cannot come from SQL, so their cost is worth pinning."""
    client = FakeClient(rows_by_day={"2026-09-08": _rows(82, 1)})
    run_dsa_collection(platforms_file, days=1, client=client, end=END, db_path=db)
    assert len(client.sql_calls) == 2, "counts and action-date spread, all platforms"
    assert len(client.count_calls) == 4, "2 platforms x (DE total, DE Article 16)"
    assert any("territorial_scope:DE" in q and ARTICLE_16 in q
               for q in client.count_calls)


def test_batch_dump_records_how_far_back_its_actions_reach(db, platforms_file):
    """A back-catalogue dump must not be storable as one day of enforcement."""
    client = FakeClient(
        rows_by_day={"2026-09-08": _rows(78, 209921)},
        spreads={"2026-09-08": [(78, "2024-02-26T00:00:00.000Z",
                                 "2026-07-31T00:00:00.000Z", 221)]})
    run_dsa_collection(platforms_file, days=1, client=client, end=END, db_path=db)
    with session(db) as conn:
        row = conn.execute("SELECT title, payload FROM raw_item "
                           "WHERE source_slug = ?", ("dsa-shein",)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["action_dates"] == {"earliest": "2024-02-26",
                                       "latest": "2026-07-31",
                                       "distinct_days": 221}
    assert "actions over 221 days" in row["title"]


def test_a_failed_day_stops_the_watermark_before_the_hole(db, platforms_file):
    client = FakeClient(
        rows_by_day={d: _rows(82, 1) for d in
                     ("2026-09-06", "2026-09-07", "2026-09-08")},
        fail_days={"2026-09-07"})
    s = run_dsa_collection(platforms_file, days=3, client=client, end=END, db_path=db)

    assert s["failed_days"] == ["2026-09-07"]
    with session(db) as conn:
        mark = get_watermark(conn, "collection:dsa")
    assert mark == "2026-09-06", "must not advance past the missing day"


def test_the_day_after_a_failure_is_still_collected(db, platforms_file):
    """A mid-window failure must not cost the days after it."""
    client = FakeClient(
        rows_by_day={d: _rows(82, 1) for d in ("2026-09-07", "2026-09-08")},
        fail_days={"2026-09-07"})
    run_dsa_collection(platforms_file, days=2, client=client, end=END, db_path=db)
    with session(db) as conn:
        days = [r["external_id"] for r in conn.execute(
            "SELECT DISTINCT external_id FROM raw_item ORDER BY external_id")]
    assert days == ["2026-09-08"]


def test_a_hole_is_refilled_on_the_next_run(db, platforms_file):
    rows = {d: _rows(82, 1) for d in ("2026-09-07", "2026-09-08")}
    broken = FakeClient(rows_by_day=rows, fail_days={"2026-09-07"})
    run_dsa_collection(platforms_file, days=2, client=broken, end=END, db_path=db)

    healed = FakeClient(rows_by_day=rows)
    s = run_dsa_collection(platforms_file, days=2, client=healed, end=END, db_path=db)
    with session(db) as conn:
        mark = get_watermark(conn, "collection:dsa")
        days = [r["external_id"] for r in conn.execute(
            "SELECT DISTINCT external_id FROM raw_item ORDER BY external_id")]
    assert days == ["2026-09-07", "2026-09-08"]
    assert mark == "2026-09-08"
    assert s["stored"] == 2, "only the previously-missing day is new"


def test_batch_filer_zero_days_are_stored_and_reported_as_yield(db, platforms_file):
    """Shein files nothing most days. That is data, not a failed collection."""
    client = FakeClient(rows_by_day={"2026-09-08": _rows(82, 10)})
    s = run_dsa_collection(platforms_file, days=1, client=client, end=END, db_path=db)
    shein = next(r for r in s["per_source"] if r["slug"] == "dsa-shein")
    assert shein["status"] == "ok"
    with session(db) as conn:
        row = conn.execute("SELECT payload FROM raw_item WHERE source_slug = ?",
                           ("dsa-shein",)).fetchone()
    assert json.loads(row["payload"])["total"] == 0
