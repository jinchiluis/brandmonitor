"""Pinned sitemap discovery: exactly the configured files, and loud when one breaks.

The whole risk of pinning is a source that stops yielding without anything
failing, so these tests are mostly about the difference between a file that could
not be read (the source fails, its watermark holds) and a file that was read and
happened to be empty (a Sunday).
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import run_collection, source_watermark_scope  # noqa: E402
from src.db import get_watermark, migrate, session  # noqa: E402
from src.discovery import (  # noqa: E402
    PinnedSitemapError, collect_from_pinned, expand_dates, has_pins,
)
from src.polite_http import DiscoveryCache  # noqa: E402
from vendor.newscrawler import crawler  # noqa: E402
from vendor.newscrawler.crawler import BERLIN_TZ  # noqa: E402

END = datetime(2026, 9, 16, 12, 0, tzinfo=BERLIN_TZ)
SINCE = END - timedelta(days=2)


def urlset(*locs: str, when: str = "2026-09-16T08:00:00+02:00") -> bytes:
    """A sitemap listing each loc with a <lastmod> inside the window."""
    rows = "".join(
        f"<url><loc>{loc}</loc><lastmod>{when}</lastmod></url>"
        for loc in locs)
    return (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows.encode() + b"</urlset>")


def index(*children: str) -> bytes:
    rows = "".join(f"<sitemap><loc>{child}</loc></sitemap>" for child in children)
    return (b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows.encode() + b"</sitemapindex>")


EMPTY = urlset()


class Server:
    """Stands in for the network under every requests adapter.

    A file may be ``bytes``, or ``(status, body)``, or ``("redirect", final_url,
    body)`` to answer 200 from a different host than the one asked for.
    """

    def __init__(self, files, etag=False):
        self.files = files
        self.etag = etag
        self.log = []

    def respond(self, request):
        r = requests.Response()
        r.request, r.url = request, request.url
        r.headers = CaseInsensitiveDict({"Content-Type": "application/xml"})
        entry = self.files.get(request.url)
        if entry is None:
            r.status_code, r._content = 404, b"not found"
        elif isinstance(entry, tuple) and entry[0] == "redirect":
            r.status_code, r.url, r._content = 200, entry[1], entry[2]
        elif isinstance(entry, tuple):
            r.status_code, r._content = entry
        else:
            tag = f'"{hash(entry)}"'
            if self.etag and request.headers.get("If-None-Match") == tag:
                r.status_code, r._content = 304, b""
            else:
                r.status_code, r._content = 200, entry
                if self.etag:
                    r.headers["ETag"] = tag
        r._content_consumed = True
        self.log.append((request.url, r.status_code))
        return r


@pytest.fixture
def server(monkeypatch):
    def install(files, **kwargs):
        srv = Server(files, **kwargs)
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                            lambda self, request, **kw: srv.respond(request))
        return srv

    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)

    def no_discovery(*args, **kwargs):
        raise AssertionError("a pinned source must not discover sitemaps")

    monkeypatch.setattr(crawler, "discover_sitemaps", no_discovery)
    return install


def entry(**over):
    base = {"url": "https://x.de/", "organization": "X", "sitemap": True}
    base.update(over)
    return base


def collect(src, **kwargs):
    return collect_from_pinned(src, SINCE, END, session=requests.Session(), **kwargs)


class TestExpandDates:
    """A rolling filename is pinnable because the window resolves it, not discovery."""

    def test_a_url_without_tokens_is_returned_unchanged(self):
        assert expand_dates("https://x.de/news.xml", SINCE, END) == \
            ["https://x.de/news.xml"]

    def test_month_tokens_resolve_against_the_window(self):
        assert expand_dates("https://x.de/{YYYY}/{MM}/s.xml", SINCE, END) == \
            ["https://x.de/2026/09/s.xml"]

    def test_a_month_boundary_resolves_both_months_newest_first(self):
        end = datetime(2026, 10, 1, 6, 0, tzinfo=BERLIN_TZ)
        since = end - timedelta(hours=48)
        assert expand_dates("https://x.de/{YYYY}/{MM}/s.xml", since, end) == [
            "https://x.de/2026/10/s.xml",
            "https://x.de/2026/09/s.xml",
        ]

    def test_a_year_boundary_resolves_both_years(self):
        end = datetime(2027, 1, 1, 6, 0, tzinfo=BERLIN_TZ)
        since = end - timedelta(hours=48)
        assert expand_dates("https://x.de/{YYYY}/{MM}/s.xml", since, end) == [
            "https://x.de/2027/01/s.xml",
            "https://x.de/2026/12/s.xml",
        ]

    def test_day_tokens_resolve_one_file_per_day(self):
        assert expand_dates("https://x.de/s-{YYYY}{MM}{DD}.xml", SINCE, END) == [
            "https://x.de/s-20260916.xml",
            "https://x.de/s-20260915.xml",
            "https://x.de/s-20260914.xml",
        ]

    def test_a_year_only_template_is_not_resolved_twice_for_one_year(self):
        since = datetime(2026, 1, 1, tzinfo=BERLIN_TZ)
        assert expand_dates("https://x.de/{YYYY}.xml", since, END) == \
            ["https://x.de/2026.xml"]


class TestPinnedFetch:
    """Exactly the configured files, filtered exactly as the walker filters."""

    def test_only_the_pinned_files_are_fetched(self, server):
        srv = server({
            "https://x.de/news.xml": urlset("https://x.de/a"),
            "https://x.de/wirtschaft.xml": urlset("https://x.de/b"),
            "https://x.de/archive.xml": urlset("https://x.de/old"),
        })
        hints = collect(entry(sitemap_urls=["https://x.de/news.xml",
                                            "https://x.de/wirtschaft.xml"]))
        assert sorted(h.url for h in hints) == ["https://x.de/a", "https://x.de/b"]
        assert [url for url, _status in srv.log] == [
            "https://x.de/news.xml", "https://x.de/wirtschaft.xml"]

    def test_entries_outside_the_window_and_the_allowed_dirs_are_dropped(self, server):
        body = (b'<?xml version="1.0"?>'
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://x.de/politik/keep</loc>"
                b"<lastmod>2026-09-16T08:00:00+02:00</lastmod></url>"
                b"<url><loc>https://x.de/sport/wrong-dir</loc>"
                b"<lastmod>2026-09-16T08:00:00+02:00</lastmod></url>"
                b"<url><loc>https://x.de/politik/too-old</loc>"
                b"<lastmod>2026-08-01T08:00:00+02:00</lastmod></url>"
                b"</urlset>")
        server({"https://x.de/news.xml": body})
        hints = collect(entry(sitemap_urls=["https://x.de/news.xml"],
                              allowed_dirs=["politik"]))
        assert [h.url for h in hints] == ["https://x.de/politik/keep"]

    def test_an_undated_entry_is_skipped_rather_than_guessed(self, server):
        body = (b'<?xml version="1.0"?>'
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://x.de/undated</loc></url></urlset>")
        server({"https://x.de/news.xml": body})
        assert collect(entry(sitemap_urls=["https://x.de/news.xml"])) == []

    def test_a_readable_but_empty_sitemap_is_not_an_error(self, server):
        """Sunday. The discriminator is readable, never full."""
        server({"https://x.de/news.xml": EMPTY})
        assert collect(entry(sitemap_urls=["https://x.de/news.xml"])) == []

    def test_a_304_replays_the_stored_entries(self, server):
        srv = server({"https://x.de/news.xml": urlset("https://x.de/a")}, etag=True)
        src = entry(sitemap_urls=["https://x.de/news.xml"])
        cache = DiscoveryCache({}, SINCE)
        assert [h.url for h in collect(src, cache=cache)] == ["https://x.de/a"]

        rows = {url: dict(row, url=url) for url, row in cache.updates.items()}
        replayed = DiscoveryCache(rows, SINCE)
        assert [h.url for h in collect(src, cache=replayed)] == ["https://x.de/a"]
        assert srv.log[-1][1] == 304
        assert replayed.replayed == 1


class TestAPinThatBreaks:
    """A pin is the only place a source's articles come from, so silence is a fault."""

    @pytest.mark.parametrize("body,reason", [
        (None, "404"),
        ((500, b"boom"), "500"),
        ((200, b"<html><body>Access denied</body></html>"), "HTML"),
        ((200, b""), "empty body"),
        ((200, b"not xml at all"), "unparseable"),
        # Parses, but into something that is not a sitemap: the quiet failure
        # worth catching, since it keeps answering 200 with no entries.
        ((200, b"<rss><channel><title>now a feed</title></channel></rss>"),
         "not a sitemap"),
    ])
    def test_an_unreadable_pin_raises(self, server, body, reason):
        server({} if body is None else {"https://x.de/news.xml": body})
        with pytest.raises(PinnedSitemapError, match="pinned sitemap unavailable"):
            collect(entry(sitemap_urls=["https://x.de/news.xml"]))

    def test_a_redirect_to_another_host_raises(self, server):
        server({"https://x.de/news.xml": ("redirect", "https://parked.example/ad",
                                          urlset("https://parked.example/a"))})
        with pytest.raises(PinnedSitemapError, match="redirected off host"):
            collect(entry(sitemap_urls=["https://x.de/news.xml"]))

    def test_a_redirect_between_www_and_bare_host_is_fine(self, server):
        server({"https://x.de/news.xml": ("redirect", "https://www.x.de/news.xml",
                                          urlset("https://x.de/a"))})
        assert [h.url for h in collect(entry(sitemap_urls=["https://x.de/news.xml"]))] \
            == ["https://x.de/a"]

    def test_one_missing_file_of_a_month_boundary_group_is_not_a_failure(self, server):
        """The file for a month that has just begun may legitimately not exist."""
        end = datetime(2026, 10, 1, 6, 0, tzinfo=BERLIN_TZ)
        server({"https://x.de/2026/09/s.xml": urlset("https://x.de/september",
                                                     when="2026-09-30T08:00:00+02:00")})
        hints = collect_from_pinned(entry(sitemap_urls=["https://x.de/{YYYY}/{MM}/s.xml"]),
                                    end - timedelta(hours=48), end,
                                    session=requests.Session())
        assert [h.url for h in hints] == ["https://x.de/september"]

    def test_a_group_with_nothing_readable_still_raises(self, server):
        end = datetime(2026, 10, 1, 6, 0, tzinfo=BERLIN_TZ)
        server({})
        with pytest.raises(PinnedSitemapError):
            collect_from_pinned(entry(sitemap_urls=["https://x.de/{YYYY}/{MM}/s.xml"]),
                                end - timedelta(hours=48), end,
                                session=requests.Session())


class TestLatest:
    """A page number that rolls stays pinnable for one extra request."""

    FILES = {
        "https://x.de/sitemap.xml": index("https://x.de/s/2.xml",
                                          "https://x.de/s/10.xml",
                                          "https://x.de/s/9.xml",
                                          "https://x.de/other.xml"),
        "https://x.de/s/10.xml": urlset("https://x.de/newest"),
        "https://x.de/s/9.xml": urlset("https://x.de/older"),
        "https://x.de/s/2.xml": urlset("https://x.de/ancient"),
    }

    def pin(self, **over):
        spec = {"url": "https://x.de/s/{LATEST}.xml", "index": "https://x.de/sitemap.xml"}
        spec.update(over)
        return entry(sitemap_urls=[spec])

    def test_the_highest_number_wins_not_the_last_listed(self, server):
        server(self.FILES)
        assert [h.url for h in collect(self.pin())] == ["https://x.de/newest"]

    def test_latest_count_takes_the_newest_n(self, server):
        server(self.FILES)
        hints = collect(self.pin(latest_count=2))
        assert sorted(h.url for h in hints) == ["https://x.de/newest", "https://x.de/older"]

    def test_an_index_that_lists_no_matching_child_raises(self, server):
        server({"https://x.de/sitemap.xml": index("https://x.de/other.xml")})
        with pytest.raises(PinnedSitemapError, match="lists no child"):
            collect(self.pin())

    def test_an_unreadable_index_raises(self, server):
        server({})
        with pytest.raises(Exception):
            collect(self.pin())

    def test_latest_without_an_index_is_a_configuration_error(self, server):
        server({})
        with pytest.raises(PinnedSitemapError, match="needs an"):
            collect(entry(sitemap_urls=["https://x.de/s/{LATEST}.xml"]))


class TestHasPins:
    def test_pins_apply_only_to_a_source_whose_sitemap_method_is_on(self):
        assert has_pins(entry(sitemap_urls=["https://x.de/n.xml"]))
        assert not has_pins(entry(sitemap=False, sitemap_urls=["https://x.de/n.xml"]))
        assert not has_pins(entry())


class TestSourceStatus:
    """What the run makes of it: a broken pin must hold the source's watermark."""

    def project(self, tmp_path, pins, **over):
        db = tmp_path / "test.sqlite3"
        migrate(db)
        source_file = tmp_path / "sources.json"
        source_file.write_text(json.dumps([entry(sitemap_urls=pins, **over)]),
                               encoding="utf-8")
        return db, source_file

    def marks(self, db):
        with session(db) as conn:
            return get_watermark(conn, source_watermark_scope("news", "x.de"))

    def test_a_broken_pin_fails_the_source_and_holds_its_watermark(self, server, tmp_path):
        server({})
        db, source_file = self.project(tmp_path, ["https://x.de/news.xml"])
        summary = run_collection(source_file, db_path=db, workers=1)
        row = summary["per_source"][0]

        assert (summary["failed"], row["status"]) == (1, "failed")
        assert "pinned sitemap unavailable" in row["error"]
        assert row["watermark_advanced"] is False
        # Still at the window it was given, so the next pass re-covers it exactly.
        assert self.marks(db) == summary["start"]

    def test_a_readable_empty_pin_leaves_the_source_fine_and_advances(self, server, tmp_path):
        server({"https://x.de/news.xml": EMPTY})
        db, source_file = self.project(tmp_path, ["https://x.de/news.xml"])
        summary = run_collection(source_file, db_path=db, workers=1)
        row = summary["per_source"][0]

        assert (summary["failed"], row["status"]) == (0, "zero")
        assert row["error"] is None
        assert self.marks(db) == summary["end"]

    def test_a_pinned_source_sends_no_homepage_probe(self, server, tmp_path):
        srv = server({"https://x.de/news.xml": urlset("https://x.de/a")})
        db, source_file = self.project(tmp_path, ["https://x.de/news.xml"])
        summary = run_collection(source_file, db_path=db, workers=1)

        assert summary["per_source"][0]["status"] == "ok"
        assert [url for url, _status in srv.log] == ["https://x.de/news.xml"]

    def test_an_unpinned_source_still_walks_its_index(self, monkeypatch, tmp_path):
        """The walker is demoted, not removed: a source without pins is unchanged."""
        walked = []
        monkeypatch.setattr("src.collect.pick_accessible_origin", lambda s, u: "https://x.de")

        def walk(session_, site_url, since, end, **kwargs):
            walked.append(site_url)
            return []

        monkeypatch.setattr("src.collect.collect_from_sitemaps", walk)
        db = tmp_path / "test.sqlite3"
        migrate(db)
        source_file = tmp_path / "sources.json"
        source_file.write_text(json.dumps([entry()]), encoding="utf-8")
        run_collection(source_file, db_path=db, workers=1)

        assert walked == ["https://x.de/"]
