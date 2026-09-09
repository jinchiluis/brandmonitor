"""Tests for the storage layer.

These cover the guarantees mvp_plan asks for: migrations are idempotent, re-running
a window stores no duplicates, zero yield is distinguishable from failure, and
collection and analysis watermarks move independently.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (  # noqa: E402
    finish_run, get_watermark, migrate, record_source_result, session,
    set_watermark, start_run,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.sqlite3"
    migrate(path)
    return path


class TestMigrations:
    def test_creates_the_expected_tables(self, db):
        with session(db) as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"run", "run_source", "raw_item", "assessment",
                "report", "watermark"} <= names

    def test_is_idempotent(self, db):
        assert migrate(db) == []

    def test_records_what_it_applied(self, db):
        with session(db) as conn:
            rows = list(conn.execute("SELECT name FROM schema_migration"))
        assert any(r["name"].startswith("001") for r in rows)


class TestWatermarks:
    def test_absent_scope_reads_none(self, db):
        with session(db) as conn:
            assert get_watermark(conn, "collection:news") is None

    def test_roundtrip_and_overwrite(self, db):
        with session(db) as conn:
            set_watermark(conn, "collection:news", "2026-09-01T00:00:00")
            set_watermark(conn, "collection:news", "2026-09-08T00:00:00")
            assert get_watermark(conn, "collection:news") == "2026-09-08T00:00:00"

    def test_collection_and_analysis_are_independent(self, db):
        """mvp_plan requires these to advance separately."""
        with session(db) as conn:
            set_watermark(conn, "collection:news", "2026-09-08T00:00:00")
            set_watermark(conn, "analysis:jt-express:news", "2026-09-01T00:00:00")
            assert get_watermark(conn, "collection:news") != \
                   get_watermark(conn, "analysis:jt-express:news")


class TestRunAccounting:
    def test_zero_yield_is_not_failure(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "2026-09-01", "2026-09-08")
            record_source_result(conn, rid, "quiet.de", "zero", items_found=0)
            record_source_result(conn, rid, "broken.de", "failed", error="boom")
            rows = {r["source_slug"]: r for r in conn.execute(
                "SELECT * FROM run_source WHERE run_id = ?", (rid,))}
        assert rows["quiet.de"]["status"] == "zero"
        assert rows["quiet.de"]["error"] is None
        assert rows["broken.de"]["status"] == "failed"
        assert rows["broken.de"]["error"] == "boom"

    def test_re_recording_a_source_updates_in_place(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "2026-09-01", "2026-09-08")
            record_source_result(conn, rid, "x.de", "failed", error="timeout")
            record_source_result(conn, rid, "x.de", "ok", items_found=5, items_stored=5)
            rows = list(conn.execute(
                "SELECT * FROM run_source WHERE run_id = ?", (rid,)))
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["error"] is None

    def test_finish_run_sets_status(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "2026-09-01", "2026-09-08")
            finish_run(conn, rid, "ok", note="12 new items")
            row = conn.execute("SELECT * FROM run WHERE id = ?", (rid,)).fetchone()
        assert row["status"] == "ok"
        assert row["finished_at"] is not None


class TestRawItems:
    def _insert(self, conn, run_id, external_id, title="t"):
        return conn.execute(
            "INSERT OR IGNORE INTO raw_item (source_slug, source_kind, external_id, "
            "version, url, title, published_at, fetched_at, first_run_id, "
            "content_hash, payload) VALUES ('x.de','news',?,1,?,?, '2026-09-01', "
            "'2026-09-01T00:00:00', ?, 'h', '{}')",
            (external_id, external_id, title, run_id),
        ).rowcount

    def test_same_item_is_not_stored_twice(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            first = self._insert(conn, rid, "https://x.de/a-one")
            second = self._insert(conn, rid, "https://x.de/a-one")
            count = conn.execute("SELECT COUNT(*) c FROM raw_item").fetchone()["c"]
        assert (first, second, count) == (1, 0, 1)

    def test_a_second_version_is_allowed(self, db):
        """Versioned raw items: the same URL can be re-captured as version 2."""
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            self._insert(conn, rid, "https://x.de/a-one")
            conn.execute(
                "INSERT INTO raw_item (source_slug, source_kind, external_id, version,"
                " url, title, fetched_at, first_run_id, content_hash, payload) "
                "VALUES ('x.de','news','https://x.de/a-one',2,'https://x.de/a-one',"
                "'changed','2026-09-02T00:00:00',?,'h2','{}')", (rid,))
            count = conn.execute("SELECT COUNT(*) c FROM raw_item").fetchone()["c"]
        assert count == 2

    def test_assessment_is_unique_per_client_and_version(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            self._insert(conn, rid, "https://x.de/a-one")
            item = conn.execute("SELECT id FROM raw_item").fetchone()["id"]
            for _ in range(2):
                conn.execute(
                    "INSERT OR REPLACE INTO assessment (raw_item_id, client_slug, "
                    "prompt_version, profile_version, created_at, relevant, payload) "
                    "VALUES (?, 'jt-express', 'v1', 'p1', '2026-09-01', 1, '{}')",
                    (item,))
            count = conn.execute("SELECT COUNT(*) c FROM assessment").fetchone()["c"]
        assert count == 1

    def test_two_clients_assess_the_same_item_separately(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            self._insert(conn, rid, "https://x.de/a-one")
            item = conn.execute("SELECT id FROM raw_item").fetchone()["id"]
            for client in ("jt-express", "other-client"):
                conn.execute(
                    "INSERT INTO assessment (raw_item_id, client_slug, prompt_version,"
                    " profile_version, created_at, relevant, payload) "
                    "VALUES (?, ?, 'v1', 'p1', '2026-09-01', 1, '{}')", (item, client))
            count = conn.execute("SELECT COUNT(*) c FROM assessment").fetchone()["c"]
        assert count == 2
