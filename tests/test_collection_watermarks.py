"""Per-source discovery checkpoints and their legacy transition."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import run_collection, source_watermark_scope  # noqa: E402
from src.db import (  # noqa: E402
    get_watermark, migrate, record_source_result, session, set_watermark,
    start_run,
)
from vendor.newscrawler.crawler import ArticleHint  # noqa: E402


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    source_file = tmp_path / "sources.json"
    source_file.write_text(json.dumps([
        {"url": "https://a.test/", "organization": "A", "sitemap": True},
        {"url": "https://b.test/", "organization": "B", "sitemap": True},
    ]), encoding="utf-8")
    return db, source_file


def marks(db, *slugs):
    with session(db) as conn:
        return {slug: get_watermark(conn, source_watermark_scope("news", slug))
                for slug in slugs}


def test_successful_source_advances_while_failed_source_retries_exact_window(
        project, monkeypatch):
    db, source_file = project
    calls = []
    fail_b = True

    def collect(entry, start, end, _limit, **_kwargs):
        nonlocal fail_b
        slug = entry["url"].split("/")[2]
        calls.append((slug, start, end))
        if slug == "b.test" and fail_b:
            return [], "feed: timeout"
        return [], None

    monkeypatch.setattr("src.collect.collect_source", collect)
    first = run_collection(source_file, db_path=db, workers=2)
    first_calls = {slug: start for slug, start, _end in calls}
    first_marks = marks(db, "a.test", "b.test")

    assert first["failed"] == 1
    assert first_marks == {"a.test": first["end"], "b.test": first["start"]}
    with session(db) as conn:
        assert get_watermark(conn, "collection:news") is None

    calls.clear()
    fail_b = False
    second = run_collection(source_file, db_path=db, workers=2)
    second_calls = {slug: start for slug, start, _end in calls}

    assert second_calls["a.test"] == datetime.fromisoformat(first["end"])
    assert second_calls["b.test"] == first_calls["b.test"]
    assert marks(db, "a.test", "b.test") == {
        "a.test": second["end"], "b.test": second["end"]}
    with session(db) as conn:
        assert get_watermark(conn, "collection:news") == second["end"]


def test_partial_results_are_stored_without_advancing_that_source(project, monkeypatch):
    db, source_file = project
    # Keep this test to one source so its baseline is also the run's start.
    source_file.write_text(json.dumps([
        {"url": "https://a.test/", "organization": "A", "sitemap": True},
    ]), encoding="utf-8")
    calls = []

    def partial(_entry, start, _end, _limit, **_kwargs):
        calls.append(start)
        return [ArticleHint("https://a.test/story", start, "Story", "sitemap")], \
            "feed: timeout"

    monkeypatch.setattr("src.collect.collect_source", partial)
    first = run_collection(source_file, db_path=db, workers=1)
    assert first["stored"] == 1
    assert marks(db, "a.test")["a.test"] == first["start"]

    monkeypatch.setattr(
        "src.collect.collect_source",
        lambda _entry, start, _end, _limit, **_kwargs: calls.append(start) or ([], None),
    )
    second = run_collection(source_file, db_path=db, workers=1)
    assert calls == [datetime.fromisoformat(first["start"])] * 2
    assert marks(db, "a.test")["a.test"] == second["end"]


def test_legacy_source_inherits_global_mark_but_new_source_gets_lookback(
        project, monkeypatch):
    db, source_file = project
    source_file.write_text(json.dumps([
        {"url": "https://old.test/", "organization": "Old", "sitemap": True},
        {"url": "https://new.test/", "organization": "New", "sitemap": True},
    ]), encoding="utf-8")
    legacy = "2026-09-01T12:00:00+02:00"
    with session(db) as conn:
        run_id = start_run(conn, "news", "2026-08-01T00:00:00+02:00", legacy)
        record_source_result(conn, run_id, "old.test", "ok")
        set_watermark(conn, "collection:news", legacy)

    calls = {}

    def collect(entry, start, end, _limit, **_kwargs):
        calls[entry["url"].split("/")[2]] = (start, end)
        return [], None

    monkeypatch.setattr("src.collect.collect_source", collect)
    run_collection(source_file, db_path=db, workers=1)

    assert calls["old.test"][0] == datetime.fromisoformat(legacy)
    assert timedelta(days=29, hours=23) < \
        calls["new.test"][1] - calls["new.test"][0] <= timedelta(days=30)


def test_days_gap_does_not_advance_source_checkpoint(project, monkeypatch):
    db, source_file = project
    source_file.write_text(json.dumps([
        {"url": "https://a.test/", "organization": "A", "sitemap": True},
    ]), encoding="utf-8")
    old = (datetime.now().astimezone() - timedelta(days=10)).isoformat()
    with session(db) as conn:
        set_watermark(conn, source_watermark_scope("news", "a.test"), old)

    monkeypatch.setattr("src.collect.collect_source", lambda *_args, **_kwargs: ([], None))
    summary = run_collection(source_file, days=1, db_path=db, workers=1)

    assert not summary["watermark_advanced"]
    assert not summary["per_source"][0]["watermark_advanced"]
    assert marks(db, "a.test")["a.test"] == old
