import json
from datetime import datetime, timedelta, timezone

from health.analyze import analyze
from src.db import (finish_run, migrate, record_source_result, session,
                    set_watermark, start_run)


UTC = timezone.utc


def _json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _inputs(tmp_path, *, critical=True):
    news = _json(tmp_path / "news.json", [{
        "url": "https://news.test/", "organization": "News Test",
        "sitemap": True,
    }])
    regulatory = _json(tmp_path / "regulatory.json", [])
    canary_config = _json(tmp_path / "canaries.json", {
        "schema_version": 1,
        "checks": [{
            "id": "news-test",
            "source_slug": "news.test",
            "kind": "sitemap",
            "url": "https://news.test/sitemap.xml",
            "failure_severity": "critical" if critical else "warning",
        }],
    })
    return news, regulatory, canary_config


def _add_news_run(db, day, found, status="ok"):
    start = datetime(2026, 9, 1, 4, tzinfo=UTC) + timedelta(days=day)
    end = start + timedelta(days=1)
    with session(db) as conn:
        run_id = start_run(conn, "news", start.isoformat(), end.isoformat())
        record_source_result(
            conn, run_id, "news.test", status, items_found=found,
            error="blocked" if status == "failed" else None,
        )
        if status != "failed":
            set_watermark(conn, "collection:news:news.test", end.isoformat())
        finish_run(conn, run_id, "failed" if status == "failed" else "ok")
        conn.execute(
            "UPDATE run SET started_at = ?, finished_at = ? WHERE id = ?",
            (start.isoformat(), (start + timedelta(minutes=10)).isoformat(), run_id),
        )
    return run_id


def _canary(path, run_id, status="healthy", incidents=None):
    return _json(path, {
        "schema_version": 1,
        "kind": "publisher_canaries",
        "generated_utc": "2026-09-13T05:00:00Z",
        "cycle_date": "2026-09-13",
        "cycle_id": f"2026-09-13-news-{run_id}",
        "news_run_id": run_id,
        "status": status,
        "incidents": incidents or [],
        "checks": [],
    })


def test_manual_reruns_on_one_day_do_not_train_a_daily_baseline(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    for _ in range(8):
        run_id = _add_news_run(db, 0, 12)
    news, regulatory, canary_config = _inputs(tmp_path)
    canary = _canary(tmp_path / "canary.json", run_id)

    result = analyze(
        db_path=db,
        news_sources=news,
        regulatory_sources=regulatory,
        canary_config=canary_config,
        canary_file=canary,
        output_dir=tmp_path / "health",
        cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, 1, tzinfo=UTC),
    )

    assert result["status"] == "learning"
    assert result["incidents"] == []
    assert result["sources"][0]["baseline_state"] == "learning"


def test_two_zeros_against_established_baseline_are_critical_for_canary_source(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    for day in range(7):
        _add_news_run(db, day, 20)
    _add_news_run(db, 7, 0, status="zero")
    run_id = _add_news_run(db, 8, 0, status="zero")
    news, regulatory, canary_config = _inputs(tmp_path)
    canary = _canary(tmp_path / "canary.json", run_id)

    result = analyze(
        db_path=db,
        news_sources=news,
        regulatory_sources=regulatory,
        canary_config=canary_config,
        canary_file=canary,
        output_dir=tmp_path / "health",
        cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, 1, tzinfo=UTC),
    )

    assert result["status"] == "critical"
    incident = next(item for item in result["incidents"] if item["check"] == "zero_streak")
    assert incident["source"] == "news.test"
    assert "prior comparable-run median was 20" in incident["message"]
    assert result["incident_key"]


def test_missing_source_result_is_detected_without_changing_database(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    start = "2026-09-12T04:00:00+00:00"
    end = "2026-09-13T04:00:00+00:00"
    with session(db) as conn:
        run_id = start_run(conn, "news", start, end)
        finish_run(conn, run_id, "ok")
    news, regulatory, canary_config = _inputs(tmp_path)
    canary = _canary(tmp_path / "canary.json", run_id)

    result = analyze(
        db_path=db,
        news_sources=news,
        regulatory_sources=regulatory,
        canary_config=canary_config,
        canary_file=canary,
        output_dir=tmp_path / "health",
        cycle_date="2026-09-13",
    )

    assert result["status"] == "critical"
    assert any(item["check"] == "missing_source_result" for item in result["incidents"])
    with session(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_source").fetchone()[0] == 0
