"""The read-only monitor: what it derives from the records, and that it changes nothing."""

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.admin as admin  # noqa: E402
from src.db import migrate, session  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def store(tmp_path):
    data, inputs = tmp_path / "data", tmp_path / "input"
    data.mkdir()
    inputs.mkdir()
    migrate(data / "brandmonitor.sqlite3")
    (inputs / "germany_medias.json").write_text(json.dumps([
        {"url": "https://www.quiet.test/", "organization": "Quiet", "sitemap": True},
        {"url": "https://blind.test/", "organization": "Blind", "feeds": True},
    ]), encoding="utf-8")
    return admin.Store(data, inputs, tmp_path)


def add_run(db, kind, started, finished, status="ok"):
    with session(db) as conn:
        return conn.execute("INSERT INTO run (kind, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
                            (kind, started, finished, status)).lastrowid


def test_a_quiet_source_and_an_unanswered_one_are_told_apart(store):
    """The 2026-09-16 06:00 case: both were stored as zero."""
    db = store.db_path
    run = add_run(db, "news", at(60), at(55))
    with session(db) as conn:
        for slug, attempts, responses in (("quiet.test", 1, 1), ("blind.test", 3, 0)):
            conn.execute("INSERT INTO run_source (run_id, source_slug, status) VALUES (?, ?, 'zero')",
                         (run, slug))
            conn.execute("INSERT INTO discovery_source (run_id, source_slug, status, attempts, responses, "
                         "transport_errors) VALUES (?, ?, 'zero', ?, ?, ?)",
                         (run, slug, attempts, responses, attempts - responses))
        conn.execute("INSERT INTO fetch_event (run_id, source_slug, seq, at, method, url, error) "
                     "VALUES (?, 'blind.test', 1, ?, 'GET', 'https://blind.test/rss', 'ConnectionError: x')",
                     (run, at(59)))

    grid = admin.api_grid(store, {"kind": "news", "days": "1"})
    classes = {slug: by_run[str(run)]["class"] for slug, by_run in grid["cells"].items()}
    assert classes == {"quiet.test": "zero", "blind.test": "blind"}
    assert [s["organization"] for s in grid["sources"]] == ["Blind", "Quiet"]

    detail = admin.api_run(store, {"id": str(run)})
    assert {s["source_slug"]: s["class"] for s in detail["sources"]} == classes
    assert detail["events"][0]["error"].startswith("ConnectionError")


@pytest.mark.parametrize("row, expected", [
    ({"status": "failed"}, "failed"),
    ({"status": "paused"}, "paused"),
    ({"status": "zero"}, "zero"),
    ({"status": "zero", "attempts": 2, "responses": 0}, "blind"),
    ({"status": "ok", "items_stored": 3, "attempts": 2, "responses": 2}, "new"),
    ({"status": "ok", "items_stored": 0, "attempts": 2, "responses": 2}, "seen"),
    ({"status": "ok", "items_stored": 3, "attempts": 4, "responses": 0}, "partial"),
    ({"status": "ok", "items_stored": 3, "attempts": 2, "responses": 2, "bad_requests": 1}, "partial"),
    ({"status": "ok", "items_stored": 1, "error": "truncated: fetch cap"}, "truncated"),
])
def test_classification(row, expected):
    assert admin.classify(row) == expected


def test_recorded_passes_own_their_runs_and_the_rest_are_derived():
    def run(i, kind, start, minutes=2, status="ok"):
        return {"id": i, "kind": kind, "started_at": at(start), "finished_at": at(start - minutes),
                "status": status}

    runs = [run(1, "news", 300), run(2, "bodies:news", 297), run(3, "regulatory", 295),
            run(4, "news", 180), run(5, "bodies:news", 177, status="failed"),
            run(6, "news", 60), run(7, "bodies:news", 57),
            run(8, "dip", 20)]  # a manual command long after the last pass
    recorded = [{"id": 9, "kind": "intraday", "outcome": "completed", "started_at": at(61),
                 "finished_at": at(50), "worst_exit": 0, "stages": "{}", "git_dirty": None}]
    passes = admin.group_passes(recorded, runs)
    summary = [(p["id"], p["kind"], p["derived"], p["run_ids"]) for p in passes]
    assert summary == [("d1", "daily", True, [1, 2, 3]), ("d4", "intraday", True, [4, 5]),
                       (9, "intraday", False, [6, 7]), ("d8", "manual", True, [8])]
    assert passes[1]["failed_runs"] == [5]


def test_title_gate_decisions_come_from_the_jsonl(store):
    run = add_run(store.db_path, "news", at(30), at(28))
    folder = store.data / "title_gate" / "jt-express"
    folder.mkdir(parents=True)
    lines = [{"at": at(27), "run_id": run, "source": "quiet.test", "external_id": "u1",
              "decision": "keep", "fail_open": False},
             {"at": at(27), "run_id": run, "source": "quiet.test", "external_id": "u2",
              "decision": "drop", "fail_open": False},
             {"at": at(27), "run_id": run, "source": "quiet.test", "external_id": "u3",
              "decision": "keep", "fail_open": True}]
    (folder / f"{NOW.date().isoformat()}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\nnot json\n", encoding="utf-8")

    assert admin.api_run(store, {"id": str(run)})["title_gate"]["counts"] == {
        "keep": 1, "drop": 1, "fail_open": 1}
    funnel = admin.api_funnel(store, {"days": "2"})
    assert funnel["news_runs"][0]["title_gate"] == {"keep": 1, "drop": 1, "fail_open": 1}


def test_the_database_connection_cannot_write(store):
    with store.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO run (kind, started_at, status) VALUES ('x', 'y', 'ok')")


def test_logs_are_served_only_from_inside_the_log_directory(store):
    day = store.data / "log" / "2026-09-16"
    day.mkdir(parents=True)
    (day / "run_daily.txt").write_bytes(b"[news] exit=0\n")
    assert admin.api_logs(store, {})["files"][0]["name"] == "run_daily.txt"
    assert admin.api_log(store, {"date": "2026-09-16", "name": "run_daily.txt"})["text"] == "[news] exit=0\n"
    for params in ({"date": "2026-09-16", "name": "..\\..\\brandmonitor.sqlite3"},
                   {"date": "../..", "name": "brandmonitor.sqlite3"},
                   {"date": "2026-09-16", "name": "../../../input/germany_medias.json"}):
        with pytest.raises(admin.ApiError):
            admin.api_log(store, params)


def test_the_server_serves_get_and_refuses_everything_else(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), admin.make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/overview?days=1") as response:
            overview = json.loads(response.read())
        assert overview["db"]["monitoring"] is True and overview["passes"] == []
        with urllib.request.urlopen(f"{base}/") as response:
            assert b"brandmonitor" in response.read()
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(f"{base}/api/overview", data=b"x", method="POST"))
        assert refused.value.code == 405
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/api/run?id=999")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_database_without_the_monitoring_tables_still_renders(tmp_path):
    """Production before migration 007: runs only, passes derived."""
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "brandmonitor.sqlite3") as conn:
        conn.executescript((Path(__file__).resolve().parent.parent / "migrations" /
                            "001_initial.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO run (kind, started_at, finished_at, status) VALUES ('news', ?, ?, 'ok')",
                     (at(10), at(8)))
        conn.execute("INSERT INTO run_source (run_id, source_slug, status) VALUES (1, 'quiet.test', 'zero')")
    store = admin.Store(data, tmp_path / "input", tmp_path)
    overview = admin.api_overview(store, {"days": "1"})
    assert overview["db"]["monitoring"] is False
    assert [p["id"] for p in overview["passes"]] == ["d1"]
    grid = admin.api_grid(store, {"kind": "news", "days": "1"})
    assert grid["cells"]["quiet.test"]["1"]["class"] == "zero"
    assert admin.api_source(store, {"kind": "news", "slug": "quiet.test", "days": "1"})["files"] == []
