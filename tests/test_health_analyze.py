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


def _add_news_run(db, day, stored, status="ok"):
    start = datetime(2026, 9, 1, 4, tzinfo=UTC) + timedelta(days=day)
    return _add_window_run(db, start, start + timedelta(days=1), stored, status=status)


def _add_window_run(db, start, end, stored, status="ok", found=None):
    with session(db) as conn:
        run_id = start_run(conn, "news", start.isoformat(), end.isoformat())
        record_source_result(
            conn, run_id, "news.test", status,
            items_found=stored if found is None else found, items_stored=stored,
            error="blocked" if status == "failed" else None,
        )
        # Collection holds the watermark of a failed or paused source.
        if status not in ("failed", "paused"):
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
    for day in range(9):  # 2026-09-01 is a Tuesday; days 4 and 5 are a weekend
        _add_news_run(db, day, 20)
    _add_news_run(db, 9, 0, status="zero")
    run_id = _add_news_run(db, 10, 0, status="zero")
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
    assert "prior comparable-day median was 20" in incident["message"]
    assert result["incident_key"]


def _analyze(tmp_path, db, run_id):
    news, regulatory, canary_config = _inputs(tmp_path)
    return analyze(
        db_path=db,
        news_sources=news,
        regulatory_sources=regulatory,
        canary_config=canary_config,
        canary_file=_canary(tmp_path / "canary.json", run_id),
        output_dir=tmp_path / "health",
        cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, 1, tzinfo=UTC),
    )


def test_light_runs_sum_into_one_day_rather_than_reading_as_a_drop(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    for day in range(10):
        _add_news_run(db, day, 24)
    # Day 10: eight two-hour light runs, then the overnight 06:00 run closes the day.
    cursor = datetime(2026, 9, 11, 4, tzinfo=UTC)
    for _ in range(8):
        _add_window_run(db, cursor, cursor + timedelta(hours=2), 2)
        cursor += timedelta(hours=2)
    run_id = _add_window_run(db, cursor, datetime(2026, 9, 12, 4, tzinfo=UTC), 8)

    result = _analyze(tmp_path, db, run_id)

    source = result["sources"][0]
    assert source["current_day_runs"] == 9
    assert source["current_day_stored"] == 24
    assert source["latest_stored"] == 8
    assert source["baseline_median_stored"] == 24
    assert result["incidents"] == []


def test_baseline_counts_stored_items_not_repeated_front_page_links(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    # A front page lists the same 259 links every run; only a few are new.
    for day in range(9):
        _add_window_run(db, datetime(2026, 9, 1, 4, tzinfo=UTC) + timedelta(days=day),
                        datetime(2026, 9, 2, 4, tzinfo=UTC) + timedelta(days=day),
                        40, found=259)
    for day in (9, 10):
        run_id = _add_window_run(
            db, datetime(2026, 9, 1, 4, tzinfo=UTC) + timedelta(days=day),
            datetime(2026, 9, 2, 4, tzinfo=UTC) + timedelta(days=day), 3, found=259)

    result = _analyze(tmp_path, db, run_id)

    incident = next(item for item in result["incidents"] if item["check"] == "yield_drop")
    assert "stored [3, 3]; prior median was 40" in incident["message"]


def test_a_paused_source_is_neither_an_incident_nor_still_learning(tmp_path):
    """zeit.de switched off after a block: silence is the configuration, not the source."""
    db = tmp_path / "db.sqlite3"
    migrate(db)
    for day in range(9):
        _add_news_run(db, day, 20)
    for day in (9, 10):
        run_id = _add_news_run(db, day, 0, status="paused")

    result = _analyze(tmp_path, db, run_id)

    assert result["incidents"] == []
    assert result["status"] == "healthy"
    source = result["sources"][0]
    assert source["latest_status"] == "paused"
    assert source["recent_statuses"][-2:] == ["paused", "paused"]


def test_paused_days_do_not_join_a_zero_streak_after_resuming(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    for day in range(9):
        _add_news_run(db, day, 20)
    _add_news_run(db, 9, 0, status="paused")
    run_id = _add_news_run(db, 10, 0, status="zero")

    result = _analyze(tmp_path, db, run_id)

    assert result["sources"][0]["zero_streak"] == 1
    assert not [item for item in result["incidents"] if item["check"] == "zero_streak"]


def _weekday_source(db, days, zero_days=()):
    """Days from Tuesday 2026-09-01; weekends store nothing, as trade press does."""
    run_id = None
    for day in days:
        weekend = (datetime(2026, 9, 1) + timedelta(days=day)).weekday() >= 5
        quiet = weekend or day in zero_days
        run_id = _add_news_run(db, day, 0 if quiet else 20, status="zero" if quiet else "ok")
    return run_id


def test_weekend_zeros_do_not_warn_on_monday_or_tuesday(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    # Day 12 is Sunday 09-13: the run ending Monday 06:00 Berlin covers it.
    monday_run = _weekday_source(db, range(13))
    monday = _analyze(tmp_path, db, monday_run)
    assert monday["sources"][0]["baseline_state"] == "ready"
    assert monday["sources"][0]["zero_streak"] == 0
    assert monday["incidents"] == []

    tuesday_run = _weekday_source(db, [13])
    tuesday = _analyze(tmp_path, db, tuesday_run)
    assert tuesday["sources"][0]["baseline_median_stored"] == 20
    assert tuesday["incidents"] == []


def test_zero_streak_skips_the_weekend_it_spans(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    # Zero on Thu 09-10, Fri 09-11, Sat, Sun and Mon 09-14; Tuesday's run covers Monday.
    run_id = _weekday_source(db, range(14), zero_days={9, 10, 13})

    result = _analyze(tmp_path, db, run_id)

    assert result["sources"][0]["zero_streak"] == 3
    incident = next(item for item in result["incidents"] if item["check"] == "zero_streak")
    assert "no new items for 3 consecutive days" in incident["message"]


def _ep_run(db, status, finished):
    with session(db) as conn:
        run_id = start_run(conn, "ep_procedures", "2021", "2026-09-13")
        record_source_result(conn, run_id, "oeil.secure.europarl.europa.eu",
                             status, items_found=3, items_stored=1,
                             error="listing: HTTP 503" if status == "failed" else None)
        finish_run(conn, run_id, status, note="3 of 40 due procedures, 1 versions stored")
        conn.execute("UPDATE run SET finished_at = ? WHERE id = ?",
                     (finished.isoformat(), run_id))


def _analyze_with_ep(tmp_path, db, run_id):
    news, _, canary_config = _inputs(tmp_path)
    regulatory = _json(tmp_path / "regulatory.json", [{
        "url": "https://oeil.secure.europarl.europa.eu/", "organization": "EP",
        "collector": "ep_procedures",
    }])
    return analyze(
        db_path=db, news_sources=news, regulatory_sources=regulatory,
        canary_config=canary_config, canary_file=_canary(tmp_path / "canary.json", run_id),
        output_dir=tmp_path / "health", cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, 1, tzinfo=UTC),
    )


def test_failed_collector_run_is_an_incident_and_an_ok_one_is_not(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    run_id = _add_news_run(db, 11, 12)
    _ep_run(db, "ok", datetime(2026, 9, 13, 4, 14, tzinfo=UTC))

    healthy = _analyze_with_ep(tmp_path, db, run_id)
    row = next(item for item in healthy["sources"]
               if item["source"] == "oeil.secure.europarl.europa.eu")
    assert row["kind"] == "ep_procedures" and row["latest_status"] == "ok"
    assert not [item for item in healthy["incidents"]
                if item["source"] == "oeil.secure.europarl.europa.eu"]

    _ep_run(db, "failed", datetime(2026, 9, 13, 4, 20, tzinfo=UTC))
    failed = _analyze_with_ep(tmp_path, db, run_id)
    incident = next(item for item in failed["incidents"]
                    if item["source"] == "oeil.secure.europarl.europa.eu")
    assert (incident["check"], incident["severity"]) == ("source_failed", "warning")
    assert incident["message"].endswith("failed: listing: HTTP 503")


def test_collector_without_a_recent_run_is_missing(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    run_id = _add_news_run(db, 11, 12)
    _ep_run(db, "ok", datetime(2026, 9, 11, 4, 0, tzinfo=UTC))

    result = _analyze_with_ep(tmp_path, db, run_id)

    incident = next(item for item in result["incidents"]
                    if item["source"] == "oeil.secure.europarl.europa.eu")
    assert incident["check"] == "missing_collection_run"


def test_truncated_sitemap_is_a_warning_on_an_ok_source(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    run_id = _add_news_run(db, 11, 12)
    with session(db) as conn:
        conn.execute("UPDATE run_source SET error = ? WHERE run_id = ?",
                     ("truncated: url cap 2000 reached, 4 sitemaps unread", run_id))

    result = _analyze(tmp_path, db, run_id)

    incident = next(item for item in result["incidents"] if item["check"] == "sitemap_truncated")
    assert incident["severity"] == "warning"
    assert incident["message"].endswith("early: url cap 2000 reached, 4 sitemaps unread")
    assert not any(item["check"] == "source_failed" for item in result["incidents"])


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


def test_repeated_body_failure_incident_names_the_url(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    run_id = _add_news_run(db, 0, 12)
    failed_url = "https://verbraucher.test/verbandsklagen/example"
    with session(db) as conn:
        conn.execute(
            "INSERT INTO body_fetch "
            "(source_slug, external_id, discovery_hash, hint_payload, status, attempts, "
            " attempted_at, error, last_run_id) "
            "VALUES (?, ?, 'hash', '{}', 'failed', 4, ?, 'HTTP 403', ?)",
            ("verbraucherzentrale.de", failed_url, "2026-09-12T04:00:00Z", run_id),
        )
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

    incident = next(
        item for item in result["incidents"]
        if item["check"] == "repeated_body_failures"
    )
    assert incident["source"] == "verbraucherzentrale.de"
    assert incident["message"] == f"{failed_url} remains retryable after 4 attempts"
    assert result["body_fetch"]["repeated_failures"] == [{
        "source_slug": "verbraucherzentrale.de",
        "url": failed_url,
        "attempts": 4,
        "attempted_at": "2026-09-12T04:00:00Z",
        "error": "HTTP 403",
        "last_run_id": run_id,
    }]


def test_disabled_canary_is_not_a_missing_snapshot(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    run_id = _add_news_run(db, 0, 12)
    news, regulatory, canary_config = _inputs(tmp_path)
    raw = json.loads(canary_config.read_text(encoding="utf-8"))
    raw["enabled"] = False
    _json(canary_config, raw)
    # A snapshot from when it still ran, for a different cycle: while the canary is
    # off this is neither read nor a mismatch, it is simply a leftover file.
    stale = _canary(tmp_path / "canary.json", run_id + 99)

    result = analyze(
        db_path=db,
        news_sources=news,
        regulatory_sources=regulatory,
        canary_config=canary_config,
        canary_file=stale,
        output_dir=tmp_path / "health",
        cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, 1, tzinfo=UTC),
    )

    assert result["canaries"] is None
    assert not [item for item in result["incidents"] if "canary" in item["check"]]
