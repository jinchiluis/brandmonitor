"""Conditional discovery requests and backing off a throttling host."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import collect_source  # noqa: E402
from src.db import migrate, session  # noqa: E402
from src.polite_http import (  # noqa: E402
    DiscoveryCache, PoliteAdapter, load_discovery_cache, retry_after_seconds,
    save_discovery_cache,
)
from vendor.newscrawler import crawler  # noqa: E402
from vendor.newscrawler.crawler import BERLIN_TZ  # noqa: E402
from vendor.newscrawler.crawler_html_utils import HostThrottled  # noqa: E402

T = datetime(2026, 9, 15, 12, 0, tzinfo=BERLIN_TZ)

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://x.de/news-sitemap.xml</loc><lastmod>2026-09-15T09:00:00+02:00</lastmod></sitemap>
</sitemapindex>"""

# A titled news entry, an undated entry that inherits the index <lastmod>, one
# dated after the first pass's end, and one too old for either window.
NEWS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url><loc>https://x.de/titled</loc><news:news><news:title>Titled</news:title>
    <news:publication_date>2026-09-15T08:00:00+02:00</news:publication_date></news:news></url>
  <url><loc>https://x.de/undated</loc></url>
  <url><loc>https://x.de/future</loc><news:news>
    <news:publication_date>2026-09-15T13:00:00+02:00</news:publication_date></news:news></url>
  <url><loc>https://x.de/ancient</loc><lastmod>2026-09-01T08:00:00+02:00</lastmod></url>
</urlset>"""

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
  <item><title>Feed Title</title><link>https://x.de/titled</link>
    <pubDate>Tue, 15 Sep 2026 06:00:00 GMT</pubDate></item>
  <item><title>No Date</title><link>https://x.de/nodate</link></item>
</channel></rss>"""


class Server:
    """Stands in for the network under every requests adapter."""

    def __init__(self, files, etag=True):
        self.files = files
        self.etag = etag
        self.log = []  # (url, status)

    def respond(self, request):
        r = requests.Response()
        r.request, r.url = request, request.url
        r.headers = CaseInsensitiveDict()
        if request.url in self.files:
            body = self.files[request.url]
            tag = f'"{hash(body)}"'
            if self.etag and request.headers.get("If-None-Match") == tag:
                r.status_code, r._content = 304, b""
            else:
                r.status_code, r._content = 200, body
                if self.etag:
                    r.headers["ETag"] = tag
        else:
            r.status_code, r._content = 404, b""
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
    monkeypatch.setattr(crawler, "discover_sitemaps",
                        lambda session, site_url, extra=None: ["https://x.de/sitemap.xml"])
    return install


def rules(monkeypatch, **site_rules):
    monkeypatch.setattr(crawler.sources, "get_site_rules", lambda url: site_rules)


def sitemaps(since, end, cache=None):
    return crawler.collect_from_sitemaps(requests.Session(), "https://x.de/", since, end,
                                         max_per_source=100, cache=cache)


def roundtrip(tmp_path, cache):
    """Rows as the next pass loads them, through the real table."""
    db = tmp_path / "t.sqlite3"
    migrate(db)
    with session(db) as conn:
        save_discovery_cache(conn, "x.de", cache.updates)
    with session(db) as conn:
        return load_discovery_cache(conn, "x.de")


class TestSitemapReplay:
    FILES = {"https://x.de/sitemap.xml": INDEX, "https://x.de/news-sitemap.xml": NEWS}

    def test_a_304_yields_exactly_what_a_full_download_does(self, server, monkeypatch, tmp_path):
        rules(monkeypatch)
        srv = server(self.FILES)
        first = DiscoveryCache({}, T - timedelta(hours=48))
        first_hints = sitemaps(T - timedelta(hours=48), T, first)
        assert {h.url for h in first_hints} == {"https://x.de/titled", "https://x.de/undated"}

        since, end = T - timedelta(hours=46), T + timedelta(hours=2)
        rows = roundtrip(tmp_path, first)
        srv.log.clear()
        replayed = sitemaps(since, end, DiscoveryCache(rows, since))
        assert [status for _url, status in srv.log] == [304, 304]

        srv.log.clear()
        assert replayed == sitemaps(since, end)  # no cache: full download
        # Title, inherited date and its lastmod label, and the entry dated after
        # the first pass's end all survive the replay.
        assert {(h.url, h.title, h.date_source) for h in replayed} == {
            ("https://x.de/titled", "Titled", "news_sitemap"),
            ("https://x.de/undated", None, "lastmod"),
            ("https://x.de/future", None, "news_sitemap")}

    def test_a_wider_window_than_the_stored_one_downloads_in_full(self, server, monkeypatch):
        """Entries older than the stored window were not kept, so a backfill cannot replay."""
        rules(monkeypatch)
        srv = server(self.FILES)
        first = DiscoveryCache({}, T - timedelta(hours=48))
        sitemaps(T - timedelta(hours=48), T, first)

        since = T - timedelta(days=30)
        srv.log.clear()
        hints = sitemaps(since, T, DiscoveryCache(dict(first.updates), since))
        assert [status for _url, status in srv.log] == [200, 200]
        assert "https://x.de/ancient" in {h.url for h in hints}

    def test_a_file_without_validators_is_not_cached(self, server, monkeypatch):
        rules(monkeypatch)
        server(self.FILES, etag=False)
        cache = DiscoveryCache({}, T - timedelta(hours=48))
        sitemaps(T - timedelta(hours=48), T, cache)
        assert cache.updates == {}


class TestFeedReplay:
    def test_a_304_feed_keeps_its_titles_and_undated_items(self, server, monkeypatch, tmp_path):
        rules(monkeypatch, feed_urls=["https://x.de/rss"])
        srv = server({"https://x.de/rss": FEED})
        since = T - timedelta(hours=48)
        first = DiscoveryCache({}, since)
        full = crawler.collect_from_feeds(requests.Session(), "https://x.de", cache=first)

        srv.log.clear()
        again = crawler.collect_from_feeds(requests.Session(), "https://x.de",
                                           cache=DiscoveryCache(roundtrip(tmp_path, first), since))
        assert srv.log == [("https://x.de/rss", 304)]
        assert again == full
        assert {(h.url, h.title) for h in again} == {
            ("https://x.de/titled", "Feed Title"), ("https://x.de/nodate", "No Date")}


class TestThrottle:
    def _adapter_server(self, monkeypatch, statuses, retry_after=None):
        sent = []

        def respond(self, request, **kw):
            r = requests.Response()
            r.request, r.url = request, request.url
            r.status_code = statuses[min(len(sent), len(statuses) - 1)]
            r.headers = CaseInsensitiveDict({"Retry-After": retry_after} if retry_after else {})
            r._content, r._content_consumed = b"x", True
            sent.append(request.url)
            return r

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", respond)
        return sent

    def _session(self, adapter):
        s = requests.Session()
        s.mount("https://", adapter)
        return s

    def test_one_throttle_is_waited_out_and_retried(self, monkeypatch):
        sent = self._adapter_server(monkeypatch, [429, 200], retry_after="3")
        waits = []
        adapter = PoliteAdapter(sleep=waits.append)
        r = self._session(adapter).get("https://x.de/sitemap.xml")
        assert (r.status_code, waits, len(sent), adapter.tripped) == (200, [3.0], 2, None)

    def test_a_second_throttle_stops_the_host_without_another_request(self, monkeypatch):
        sent = self._adapter_server(monkeypatch, [429, 503, 200])
        adapter = PoliteAdapter(sleep=lambda s: None)
        s = self._session(adapter)
        assert s.get("https://x.de/a").status_code == 503
        assert adapter.tripped
        with pytest.raises(HostThrottled):
            s.get("https://x.de/b")
        assert len(sent) == 2

    def test_a_wait_longer_than_it_will_sleep_stops_at_once(self, monkeypatch):
        sent = self._adapter_server(monkeypatch, [429], retry_after="3600")
        waits = []
        adapter = PoliteAdapter(sleep=waits.append)
        self._session(adapter).get("https://x.de/a")
        assert adapter.tripped and waits == [] and len(sent) == 1

    def test_retry_after_accepts_seconds_and_http_dates(self):
        assert retry_after_seconds("120") == 120.0
        assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
        assert retry_after_seconds("soon") is None and retry_after_seconds(None) is None

    def test_a_throttled_source_fails_so_its_window_is_re_covered(self, monkeypatch):
        """A throttled sitemap used to read as an empty one: ok, watermark advanced."""
        sent = self._adapter_server(monkeypatch, [429])
        monkeypatch.setattr("src.polite_http.time.sleep", lambda s: None)
        monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
        monkeypatch.setattr("src.collect.pick_accessible_origin", lambda s, u: "https://x.de")
        monkeypatch.setattr(crawler.sources, "get_site_rules", lambda url: {})
        monkeypatch.setattr(crawler, "discover_sitemaps", lambda session, site_url, extra=None: [
            f"https://x.de/sitemap-{i}.xml" for i in range(20)])
        entry = {"url": "https://x.de/", "sitemap": True, "feeds": True, "organization": "X"}
        http = {}
        hints, error = collect_source(entry, T - timedelta(hours=2), T, 100, http=http)
        assert hints == [] and error and "throttled: HTTP 429 from x.de" in error
        # Two answers, then nothing more: not the other 19 sitemaps, not the feeds.
        assert len(sent) == 2 and http["requests"] == 2
