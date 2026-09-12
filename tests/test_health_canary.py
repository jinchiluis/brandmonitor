import json
from datetime import datetime, timezone

from health.canary import Fetched, run_canaries
from src.db import finish_run, migrate, session, start_run


UTC = timezone.utc


def _db(tmp_path, urls=()):
    path = tmp_path / "brandmonitor.sqlite3"
    migrate(path)
    with session(path) as conn:
        run_id = start_run(
            conn, "news", "2026-09-12T04:00:00+00:00", "2026-09-13T04:00:00+00:00"
        )
        for index, url in enumerate(urls, 1):
            conn.execute(
                "INSERT INTO raw_item (source_slug, source_kind, external_id, version, "
                "url, title, fetched_at, first_run_id, content_hash, payload) "
                "VALUES ('news.test', 'news', ?, 1, ?, 'title', "
                "'2026-09-13T04:00:00+00:00', ?, ?, '{}')",
                (url, url, run_id, f"hash-{index}"),
            )
        finish_run(conn, run_id, "ok")
    return path, run_id


def _config(tmp_path, check):
    path = tmp_path / "canaries.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "timeout_seconds": 2,
        "max_response_bytes": 100000,
        "checks": [check],
    }), encoding="utf-8")
    return path


def test_sitemap_index_uses_highest_page_and_reconciles_database(tmp_path):
    first = "https://news.test/a"
    second = "https://news.test/b"
    db, run_id = _db(tmp_path, [first])
    config = _config(tmp_path, {
        "id": "news-sitemap",
        "source_slug": "news.test",
        "kind": "sitemap",
        "url": "https://news.test/sitemap.xml",
        "follow_sitemap_index": "highest_page",
        "minimum_entries": 1,
        "reconcile_recent": 2,
        "minimum_database_coverage": 1.0,
        "failure_severity": "critical",
        "coverage_severity": "warning",
    })
    documents = {
        "https://news.test/sitemap.xml": b"""<?xml version='1.0'?>
          <sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
            <sitemap><loc>https://news.test/leaf.xml?page=1</loc></sitemap>
            <sitemap><loc>https://news.test/leaf.xml?page=9</loc></sitemap>
          </sitemapindex>""",
        "https://news.test/leaf.xml?page=9": f"""<?xml version='1.0'?>
          <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
            <url><loc>{first}</loc><lastmod>2026-09-13</lastmod></url>
            <url><loc>{second}</loc><lastmod>2026-09-12</lastmod></url>
          </urlset>""".encode(),
    }
    calls = []

    def fetcher(url, timeout, maximum):
        calls.append(url)
        return Fetched(url, 200, "application/xml", documents[url])

    result = run_canaries(
        db_path=db,
        config_path=config,
        output_dir=tmp_path / "output",
        cycle_date="2026-09-13",
        generated_at=datetime(2026, 9, 13, 5, tzinfo=UTC),
        fetcher=fetcher,
    )

    assert calls == ["https://news.test/sitemap.xml", "https://news.test/leaf.xml?page=9"]
    assert result["news_run_id"] == run_id
    assert result["status"] == "warning"
    assert result["checks"][0]["recent_in_database"] == 1
    assert result["incidents"][0]["check"].endswith("database_coverage")
    assert (tmp_path / "output" / "latest.json").exists()
    assert (tmp_path / "output" / "history" / f"2026-09-13-news-{run_id}.json").exists()


def test_access_challenge_is_a_structural_incident_not_a_script_failure(tmp_path):
    db, _ = _db(tmp_path)
    config = _config(tmp_path, {
        "id": "blocked-feed",
        "source_slug": "news.test",
        "kind": "feed",
        "url": "https://news.test/rss.xml",
        "minimum_entries": 1,
        "failure_severity": "critical",
    })

    def fetcher(url, timeout, maximum):
        return Fetched(url, 200, "text/html", b"<title>Just a moment...</title> cf-chl-test")

    result = run_canaries(
        db_path=db,
        config_path=config,
        output_dir=tmp_path / "output",
        cycle_date="2026-09-13",
        fetcher=fetcher,
    )

    assert result["status"] == "critical"
    assert result["checks"][0]["status"] == "critical"
    assert "access-challenge" in result["incidents"][0]["message"]
