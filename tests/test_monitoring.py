"""Operational monitoring records what the pipeline did and never changes it."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as run_cli  # noqa: E402
from src.bodies import BodyResult, run_body_fetch  # noqa: E402
from src.collect import run_collection, source_watermark_scope, store_hints  # noqa: E402
from src.db import get_watermark, migrate, session, start_run  # noqa: E402
from src.monitoring import FetchRecorder, git_commit, record_pass  # noqa: E402
from src.polite_http import PoliteAdapter  # noqa: E402
from vendor.newscrawler import crawler  # noqa: E402
from vendor.newscrawler.crawler import BERLIN_TZ, ArticleHint  # noqa: E402

PINNED = "https://x.test/news-sitemap.xml"


def news_sitemap(*ages_hours):
    now = datetime.now(tz=BERLIN_TZ)
    urls = "".join(
        f"<url><loc>https://x.test/a{n}</loc><news:news><news:title>Article {n}</news:title>"
        f"<news:publication_date>{(now - timedelta(hours=age)).isoformat()}"
        f"</news:publication_date></news:news></url>"
        for n, age in enumerate(ages_hours))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            f"{urls}</urlset>").encode()


class Network:
    """Stands in for every requests adapter: files, redirects, or no network at all."""

    def __init__(self, files=None, redirects=None, down=False):
        self.files = files or {}
        self.redirects = redirects or {}
        self.down = down
        self.sent = []

    def respond(self, request):
        self.sent.append(request.url)
        if self.down:
            raise requests.ConnectionError("Failed to resolve 'x.test'")
        r = requests.Response()
        r.request, r.url = request, request.url
        r.headers = CaseInsensitiveDict()
        if request.url in self.redirects:
            r.status_code, r._content = 301, b""
            r.headers["Location"] = self.redirects[request.url]
        elif request.url in self.files:
            body = self.files[request.url]
            tag = f'"{hash(body)}"'
            if request.headers.get("If-None-Match") == tag:
                r.status_code, r._content = 304, b""
            else:
                r.status_code, r._content = 200, body
                r.headers["ETag"] = tag
                r.headers["Content-Type"] = "application/xml"
        else:
            r.status_code, r._content = 404, b""
        r._content_consumed = True
        return r


@pytest.fixture
def network(monkeypatch):
    def install(**kwargs):
        net = Network(**kwargs)
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                            lambda self, request, **kw: net.respond(request))
        return net
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    return install


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps([
        {"url": "https://x.test/", "organization": "X", "sitemap": True,
         "sitemap_urls": [PINNED]},
    ]), encoding="utf-8")
    return db, sources


def query(db, sql, *params):
    with session(db) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


class TestTransport:
    def _session(self, recorder):
        s = requests.Session()
        s.mount("https://", PoliteAdapter(recorder=recorder))
        return s

    def test_a_request_that_never_got_a_response_is_still_recorded(self, network):
        """The 2026-09-16 06:00 case: requests counted 0, so the source looked quiet."""
        network(down=True)
        recorder = FetchRecorder()
        session = self._session(recorder)
        with pytest.raises(requests.ConnectionError):
            session.get(PINNED)

        adapter = session.get_adapter(PINNED)
        assert adapter.requests == 0  # the production counter is unchanged
        [event] = recorder.events
        assert (event["url"], event["status"], event["bytes"]) == (PINNED, None, None)
        assert event["error"].startswith("ConnectionError: Failed to resolve")
        assert recorder.counts() == {"attempts": 1, "transport_errors": 1}

    def test_a_file_read_attaches_to_the_hop_that_answered(self, network):
        moved = "https://x.test/sitemaps/news.xml"
        network(files={moved: news_sitemap(1)}, redirects={PINNED: moved})
        recorder = FetchRecorder()
        response = self._session(recorder).get(PINNED)
        assert response.status_code == 200

        first, second = recorder.events
        assert (first["status"], first["url"]) == (301, PINNED)
        assert (second["status"], second["url"], second["redirected_from"]) == (
            200, moved, PINNED)
        when = datetime.now(tz=BERLIN_TZ)
        recorder.file_read(PINNED, "parsed", [("https://x.test/a", when, None, None)], [])
        assert (first["parse"], second["parse"], second["entries"]) == (None, "parsed", 1)

    def test_a_failed_read_is_not_a_parse(self, network):
        network(files={})
        recorder = FetchRecorder()
        self._session(recorder).get(PINNED)
        recorder.file_read(PINNED, "parsed", [], [])
        assert (recorder.events[0]["status"], recorder.events[0]["parse"]) == (404, None)

    def test_odd_entries_never_raise_into_the_crawl(self, network):
        network(files={PINNED: b"<urlset/>"})
        recorder = FetchRecorder(since=datetime.now(tz=timezone.utc))
        self._session(recorder).get(PINNED)
        naive, aware = datetime(2026, 9, 1), datetime.now(tz=timezone.utc)
        recorder.file_read(PINNED, "parsed", [("u", naive, None, None),
                                              ("v", aware, None, None)], [])
        recorder.file_read("never requested", "parsed", None, None)
        recorder.finish(None)


class TestDiscoveryRecords:
    def test_each_file_and_source_is_recorded_and_a_304_replays_its_counts(
            self, project, network):
        db, sources = project
        network(files={PINNED: news_sitemap(1, 5, 24 * 40)})

        first = run_collection(sources, db_path=db)
        [source] = query(db, "SELECT * FROM discovery_source WHERE run_id=?", first["run_id"])
        assert (source["status"], source["attempts"], source["responses"],
                source["transport_errors"], source["hints"], source["stored"]) == (
            "ok", 1, 1, 0, 2, 2)
        assert source["watermark_advanced"] == 1
        with session(db) as conn:
            assert source["watermark_after"] == get_watermark(
                conn, source_watermark_scope("news", "x.test")) == first["end"]
        [event] = query(db, "SELECT * FROM fetch_event WHERE run_id=?", first["run_id"])
        assert (event["status"], event["conditional"], event["parse"], event["entries"],
                event["dated_entries"], event["entries_since"]) == (200, 0, "parsed", 3, 3, 2)
        assert event["newest_entry"] > event["oldest_entry"]

        second = run_collection(sources, db_path=db)
        [event] = query(db, "SELECT * FROM fetch_event WHERE run_id=?", second["run_id"])
        # The cache kept only entries inside the first pass's window.
        assert (event["status"], event["conditional"], event["parse"], event["entries"]) == (
            304, 1, "replayed", 2)
        [source] = query(db, "SELECT * FROM discovery_source WHERE run_id=?", second["run_id"])
        assert (source["not_modified"], source["replayed"], source["stored"]) == (1, 1, 0)

    def test_an_outage_leaves_a_row_per_attempt(self, project, network):
        db, sources = project
        network(down=True)
        summary = run_collection(sources, db_path=db)
        [source] = query(db, "SELECT * FROM discovery_source WHERE run_id=?",
                         summary["run_id"])
        assert (source["status"], source["responses"], source["transport_errors"],
                source["watermark_advanced"]) == ("failed", 0, 1, 0)
        [event] = query(db, "SELECT status, error FROM fetch_event WHERE run_id=?",
                        summary["run_id"])
        assert event["status"] is None and "ConnectionError" in event["error"]

    def test_a_monitoring_failure_changes_no_outcome(self, project, network, monkeypatch):
        db, sources = project
        network(files={PINNED: news_sitemap(1, 5)})

        def broken(*args, **kwargs):
            raise RuntimeError("monitoring is broken")

        monkeypatch.setattr("src.monitoring.session", broken)
        summary = run_collection(sources, db_path=db)
        assert (summary["ok"], summary["stored"], summary["failed"]) == (1, 2, 0)
        with session(db) as conn:
            assert get_watermark(conn, source_watermark_scope("news", "x.test")) == summary["end"]
        assert query(db, "SELECT * FROM discovery_source") == []


class TestBodyAttempts:
    def test_every_attempt_is_recorded_with_what_the_queue_made_of_it(
            self, tmp_path, monkeypatch):
        db = tmp_path / "test.sqlite3"
        migrate(db)
        sources = tmp_path / "sources.json"
        sources.write_text(json.dumps([{"url": "https://trade.test/", "sitemap": True,
                                        "content_mode": "full_text"}]), encoding="utf-8")
        with session(db) as conn:
            run = start_run(conn, "news", "a", "b")
            store_hints(conn, run, "trade.test", [ArticleHint(
                "https://trade.test/article", datetime(2026, 9, 1, tzinfo=timezone.utc),
                "Headline", "sitemap")], full_text=True)

        monkeypatch.setattr("src.bodies.fetch_body",
                            lambda url: BodyResult("failed", error="ReadTimeout"))
        failed = run_body_fetch(sources, db_path=db)
        monkeypatch.setattr("src.bodies.fetch_body",
                            lambda url: BodyResult("ok", text="Body", title="Headline",
                                                   url=url))
        ok = run_body_fetch(sources, db_path=db)

        attempts = query(db, "SELECT run_id, status, error, counted, attempts, stored "
                             "FROM body_attempt ORDER BY id")
        assert attempts == [
            {"run_id": failed["run_id"], "status": "failed", "error": "ReadTimeout",
             "counted": 1, "attempts": 1, "stored": 0},
            {"run_id": ok["run_id"], "status": "ok", "error": None, "counted": 1,
             "attempts": 0, "stored": 1},
        ]


class TestPasses:
    def _repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".git" / "refs" / "heads").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / ".git" / "refs" / "heads" / "main").write_text("abc123\n")
        (root / "input").mkdir()
        (root / "config.json").write_text("{}")
        (root / "input" / "germany_medias.json").write_text("[]")
        return root

    def test_a_completed_pass_copies_its_marker(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        migrate(db)
        root = self._repo(tmp_path)
        marker = tmp_path / "last_run.json"
        # As run_daily.bat writes it: echo lines with a trailing space-free CRLF.
        marker.write_text('{\r\n  "finished_utc": "2026-09-16T04:10:57Z",\r\n'
                          '  "worst_exit": 2,\r\n  "cycle_date": "2026-09-16",\r\n'
                          '  "stages": { "netcheck": 0, "news": 0, "dip": 2 },\r\n'
                          '  "log": "data/log/2026-09-16/run_daily.txt"\r\n}\r\n')

        pass_id = record_pass(kind="daily", outcome="completed", started="2026-09-16T04:00:01",
                              marker=marker, db_path=db, root=root)
        [row] = query(db, "SELECT * FROM pipeline_pass WHERE id=?", pass_id)
        assert (row["started_at"], row["finished_at"], row["worst_exit"], row["cycle_date"]) == (
            "2026-09-16T04:00:01+00:00", "2026-09-16T04:10:57+00:00", 2, "2026-09-16")
        assert json.loads(row["stages"]) == {"netcheck": 0, "news": 0, "dip": 2}
        assert row["git_commit"] == "abc123"
        assert set(json.loads(row["config_files"])) == {"config.json",
                                                        "input/germany_medias.json"}
        assert len(row["config_hash"]) == 64

    def test_a_lock_skipped_slot_has_no_marker_and_is_still_recorded(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        migrate(db)
        pass_id = record_pass(kind="intraday", outcome="lock_skipped", started="",
                              db_path=db, root=self._repo(tmp_path))
        [row] = query(db, "SELECT * FROM pipeline_pass WHERE id=?", pass_id)
        assert (row["outcome"], row["started_at"], row["worst_exit"], row["stages"]) == (
            "lock_skipped", None, None, "{}")
        assert row["finished_at"]

    def test_a_bad_call_or_database_returns_none_rather_than_raising(self, tmp_path):
        assert record_pass(kind="weekly", outcome="completed", db_path=tmp_path / "x.db") is None
        assert record_pass(kind="daily", outcome="completed",
                           db_path=tmp_path / "unmigrated.db") is None

    def test_packed_refs_and_detached_heads(self, tmp_path):
        root = self._repo(tmp_path)
        (root / ".git" / "refs" / "heads" / "main").unlink()
        (root / ".git" / "packed-refs").write_text("# pack-refs\ndef456 refs/heads/main\n")
        assert git_commit(root) == "def456"
        (root / ".git" / "HEAD").write_text("0123abcd\n")
        assert git_commit(root) == "0123abcd"
        assert git_commit(tmp_path / "not-a-repo") is None

    def test_a_daily_pass_prunes_monitoring_rows_past_the_window_and_nothing_else(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        migrate(db)
        now = datetime.now(timezone.utc)
        old, recent = (now - timedelta(days=31)).isoformat(), (now - timedelta(days=29)).isoformat()
        with session(db) as conn:
            runs = {}
            for label, when in (("old", old), ("recent", recent)):
                run = start_run(conn, "news", when, when)
                conn.execute("UPDATE run SET started_at=? WHERE id=?", (when, run))
                runs[label] = run
                conn.execute("INSERT INTO run_source (run_id, source_slug, status) VALUES (?, 'x', 'ok')",
                             (run,))
                conn.execute("INSERT INTO discovery_source (run_id, source_slug, status) VALUES (?, 'x', 'ok')",
                             (run,))
                conn.execute("INSERT INTO fetch_event (run_id, source_slug, seq, at, method, url) "
                             "VALUES (?, 'x', 1, ?, 'GET', 'https://x.test/')", (run, when))
                conn.execute("INSERT INTO body_attempt (run_id, source_slug, external_id, at, status) "
                             "VALUES (?, 'x', 'https://x.test/a', ?, 'ok')", (run, when))
                conn.execute("INSERT INTO pipeline_pass (kind, outcome, finished_at, recorded_at) "
                             "VALUES ('daily', 'completed', ?, ?)", (when, when))

        record_pass(kind="intraday", outcome="completed", db_path=db, root=self._repo(tmp_path))
        assert len(query(db, "SELECT * FROM fetch_event")) == 2  # an intraday pass does not prune

        record_pass(kind="daily", outcome="completed", db_path=db, root=tmp_path / "repo")
        for table in ("discovery_source", "fetch_event", "body_attempt"):
            assert [r["run_id"] for r in query(db, f"SELECT run_id FROM {table}")] == [runs["recent"]]
        assert len(query(db, "SELECT * FROM pipeline_pass")) == 3  # recent + the two just written
        # The pipeline's own records are never pruned.
        assert len(query(db, "SELECT * FROM run")) == 2
        assert len(query(db, "SELECT * FROM run_source")) == 2

    def test_the_batch_command_line(self, tmp_path, monkeypatch):
        db = tmp_path / "test.sqlite3"
        monkeypatch.setattr("src.db.DB_PATH", db)
        args = run_cli.build_parser().parse_args(
            ["record-pass", "--kind", "intraday", "--outcome", "offline",
             "--started", "2026-09-16T06:00:01"])
        assert args.func(args) == 0
        [row] = query(db, "SELECT kind, outcome FROM pipeline_pass")
        assert row == {"kind": "intraday", "outcome": "offline"}
