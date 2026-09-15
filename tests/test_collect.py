"""Tests for collection storage and per-source error handling.

store_hints carries mvp_plan's dedup and versioning guarantees, and collect_source
decides whether one broken discovery method costs a source its other methods. Both
were previously proven only by a manual re-run.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import (  # noqa: E402
    collect_source, is_furniture, is_malformed, on_configured_host, slug_for,
    store_hints, url_is_excluded,
)
from src.db import migrate, session, start_run  # noqa: E402
from vendor.newscrawler.crawler import (  # noqa: E402
    BERLIN_TZ, ArticleHint, _is_news_sitemap, discover_sitemaps,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.sqlite3"
    migrate(path)
    return path


def hint(url, title="Title", when="2026-09-01T00:00:00+02:00", source="sitemap"):
    return ArticleHint(url=url, published_at=datetime.fromisoformat(when) if when else None,
                       title=title, source=source)


class TestStoreHints:
    def test_stores_new_items(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            n = store_hints(conn, rid, "x.de", [hint("https://x.de/a-one"),
                                                hint("https://x.de/a-two")])
        assert n == 2

    def test_rerunning_the_same_window_stores_nothing(self, db):
        """mvp_plan section 7: a cycle can be rerun without duplicates."""
        hints = [hint("https://x.de/a-one"), hint("https://x.de/a-two")]
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            store_hints(conn, rid, "x.de", hints)
            second = store_hints(conn, rid, "x.de", hints)
            total = conn.execute("SELECT COUNT(*) c FROM raw_item").fetchone()["c"]
        assert (second, total) == (0, 2)

    def test_changed_title_becomes_a_new_version(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title="Before")])
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title="After")])
            rows = list(conn.execute(
                "SELECT version, title FROM raw_item ORDER BY version"))
        assert [(r["version"], r["title"]) for r in rows] == [(1, "Before"), (2, "After")]

    def test_source_kind_is_recorded(self, db):
        with session(db) as conn:
            rid = start_run(conn, "regulatory", "a", "b")
            store_hints(conn, rid, "vzbv.de", [hint("https://vzbv.de/a-one")],
                        source_kind="regulatory")
            kinds = [r["source_kind"] for r in
                     conn.execute("SELECT source_kind FROM raw_item")]
        assert kinds == ["regulatory"]

    def test_tracks_are_separable(self, db):
        """The point of source_kind: select one track out of the shared store."""
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one")],
                        source_kind="news")
            store_hints(conn, rid, "vzbv.de", [hint("https://vzbv.de/b-one")],
                        source_kind="regulatory")
            reg = conn.execute(
                "SELECT COUNT(*) c FROM raw_item WHERE source_kind = 'regulatory'"
            ).fetchone()["c"]
        assert reg == 1

    def test_repeated_runs_do_not_ratchet_versions(self, db):
        """Whatever the first run writes, an identical later run must add nothing.

        The sitemap and feed copies of one article differ - no title vs title,
        lastmod vs pubDate - so comparing only against the newest version made each
        look changed relative to the other, adding rows on every run without bound.
        """
        pair = [hint("https://x.de/a-one", title=None,
                     when="2026-09-01T00:00:00+02:00"),
                hint("https://x.de/a-one", title="Real Title",
                     when="2026-09-01T06:30:00+02:00", source="rss")]
        totals = []
        with session(db) as conn:
            for _ in range(3):
                rid = start_run(conn, "news", "a", "b")
                store_hints(conn, rid, "x.de", pair)
                totals.append(
                    conn.execute("SELECT COUNT(*) c FROM raw_item").fetchone()["c"])
        assert totals[0] == totals[1] == totals[2]

    def test_a_redated_title_only_item_writes_no_version(self, db):
        """WELT and WiWo move <news:publication_date> whenever they update an
        article; BVL restamps <lastmod>. A date is not a title-only item's content."""
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            for when in ("2026-09-08T17:19:00+02:00", "2026-09-15T12:20:00+02:00"):
                store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title="Same",
                                                     when=when)])
            rows = list(conn.execute("SELECT version, published_at FROM raw_item"))
        assert [(r["version"], r["published_at"][:10]) for r in rows] == [(1, "2026-09-08")]

    def test_an_untitled_stub_gains_its_title_once_and_never_loses_it(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title=None, when=None)])
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title="Real Title",
                                                 source="rss")])
            # SZ's frontpage re-finds it untitled with a made-up 23:59:59 date.
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title=None,
                                                 when="2026-09-14T23:59:59+00:00",
                                                 source="frontpage")])
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title=" Real  Title ")])
            rows = list(conn.execute("SELECT version, title FROM raw_item ORDER BY version"))
        assert [(r["version"], r["title"]) for r in rows] == [(1, None), (2, "Real Title")]

    def test_discovery_never_writes_over_a_fetched_body(self, db):
        """A frontpage re-date wrote version 3 over an SZ body; every stage reads
        the latest version, so the body vanished from all of them."""
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", title=None)])
            conn.execute(
                "INSERT INTO raw_item (source_slug, source_kind, external_id, version, url, "
                "title, published_at, fetched_at, first_run_id, content_hash, payload) "
                "SELECT source_slug, source_kind, external_id, 2, url, 'Page Title', "
                "published_at, fetched_at, first_run_id, 'body-hash', "
                "json_set(payload, '$.body_text', 'Der Artikel.') FROM raw_item")
            stored = store_hints(conn, rid, "x.de", [
                hint("https://x.de/a-one", title="Feed Title", when="2026-09-12T14:51:00+02:00",
                     source="frontpage")])
            latest = conn.execute(
                "SELECT version, json_extract(payload, '$.body_text') body FROM raw_item "
                "ORDER BY version DESC LIMIT 1").fetchone()
        assert stored == 0 and (latest["version"], latest["body"]) == (2, "Der Artikel.")

    def test_undated_items_are_still_stored(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            n = store_hints(conn, rid, "x.de", [hint("https://x.de/a-one", when=None)])
            row = conn.execute("SELECT published_at FROM raw_item").fetchone()
        assert n == 1 and row["published_at"] is None


class TestCollectSourceErrors:
    """One failing method must not cost a source its other methods."""

    def _entry(self):
        return {"url": "https://x.de/", "sitemap": True, "feeds": True,
                "frontpage": False, "organization": "X"}

    def test_feed_failure_keeps_sitemap_results(self, monkeypatch):
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")
        monkeypatch.setattr("src.collect.collect_from_sitemaps",
                            lambda *a, **k: [hint("https://x.de/a-one")])

        def boom(*a, **k):
            raise TimeoutError("feed timed out")

        monkeypatch.setattr("src.collect.collect_from_feeds", boom)
        now = datetime.now(tz=BERLIN_TZ)
        hints, error = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        assert len(hints) == 1
        assert "feeds" in error and "TimeoutError" in error

    def test_both_failures_are_reported_together(self, monkeypatch):
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")

        def boom(*a, **k):
            raise ConnectionError("nope")

        monkeypatch.setattr("src.collect.collect_from_sitemaps", boom)
        monkeypatch.setattr("src.collect.collect_from_feeds", boom)
        now = datetime.now(tz=BERLIN_TZ)
        hints, error = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        assert hints == []
        assert "sitemap" in error and "feeds" in error

    def test_origin_failure_is_fatal_for_the_source(self, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("unreachable")

        monkeypatch.setattr("src.collect.pick_accessible_origin", boom)
        now = datetime.now(tz=BERLIN_TZ)
        hints, error = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        assert hints == [] and "origin probe failed" in error


class TestWindowOverlap:
    """A discovery date can precede the moment an article becomes visible.

    DVZ's news sitemap says only "2026-09-15" (read as 02:00) and VerkehrsRundschau
    lists an article hours after its <lastmod>. Filtering from the watermark itself
    stored nothing from either for days while both reported ok.
    """

    def _entry(self):
        return {"url": "https://x.de/", "sitemap": True, "feeds": True,
                "frontpage": True, "organization": "X"}

    def test_sitemap_and_feed_dates_are_filtered_from_before_the_watermark(self, monkeypatch):
        watermark = datetime(2026, 9, 15, 14, 0, tzinfo=BERLIN_TZ)
        end = watermark + timedelta(hours=2)
        seen = {}
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")

        def sitemaps(_session, _url, start, _end, max_per_source, report=None):
            seen["sitemap"] = start
            return []

        def frontpage(_session, _origin, start_date, cap):
            seen["frontpage"] = start_date
            return []

        monkeypatch.setattr("src.collect.collect_from_sitemaps", sitemaps)
        monkeypatch.setattr("src.collect.collect_from_frontpage", frontpage)
        monkeypatch.setattr("src.collect.collect_from_feeds", lambda *a: [
            hint("https://x.de/date-only", when="2026-09-15T02:00:00+02:00", source="rss"),
            hint("https://x.de/too-old", when="2026-09-13T13:00:00+02:00", source="rss"),
        ])

        hints, error = collect_source(self._entry(), watermark, end, 100,
                                      overlap=timedelta(hours=48))

        assert error is None
        assert seen == {"sitemap": watermark - timedelta(hours=48), "frontpage": watermark}
        assert {h.url for h in hints} == {"https://x.de/date-only"}

    def test_refinding_stored_urls_in_the_overlap_stores_nothing(self, tmp_path):
        path = tmp_path / "t.sqlite3"
        migrate(path)
        hints = [hint("https://x.de/a-one", when="2026-09-15T02:00:00+02:00")]
        with session(path) as conn:
            rid = start_run(conn, "news", "a", "b")
            first = store_hints(conn, rid, "x.de", hints)
            again = store_hints(conn, rid, "x.de", hints)
        assert (first, again) == (1, 0)


class TestHintDeduplication:
    """A source with several discovery methods finds the same article in each."""

    def _entry(self):
        return {"url": "https://x.de/", "sitemap": True, "feeds": True,
                "frontpage": False, "organization": "X"}

    def test_same_url_from_two_methods_collapses(self, monkeypatch):
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")
        monkeypatch.setattr(
            "src.collect.collect_from_sitemaps",
            lambda *a, **k: [hint("https://x.de/a-one", title=None)])
        monkeypatch.setattr(
            "src.collect.collect_from_feeds",
            lambda *a, **k: [hint("https://x.de/a-one", title="Real Title",
                                  source="rss")])
        now = datetime.now(tz=BERLIN_TZ)
        hints, error = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        assert error is None
        assert len(hints) == 1
        # The richer copy survives: a titled hint beats an untitled one.
        assert hints[0].title == "Real Title"

    def test_distinct_urls_are_both_kept(self, monkeypatch):
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")
        monkeypatch.setattr(
            "src.collect.collect_from_sitemaps",
            lambda *a, **k: [hint("https://x.de/a-one")])
        monkeypatch.setattr(
            "src.collect.collect_from_feeds",
            lambda *a, **k: [hint("https://x.de/a-two", source="rss")])
        now = datetime.now(tz=BERLIN_TZ)
        hints, _ = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        assert {h.url for h in hints} == {"https://x.de/a-one", "https://x.de/a-two"}

    def test_www_twin_is_stored_under_the_configured_host(self, monkeypatch):
        """A timed-out origin probe falls back to www; the identity must not move."""
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://www.x.de")
        monkeypatch.setattr(
            "src.collect.collect_from_sitemaps",
            lambda *a, **k: [hint("https://www.x.de/presse/a-one", title=None),
                             hint("https://sub.x.de/a-two")])
        monkeypatch.setattr(
            "src.collect.collect_from_feeds",
            lambda *a, **k: [hint("https://x.de/presse/a-one", title="Real Title",
                                  source="rss")])
        now = datetime.now(tz=BERLIN_TZ)
        hints, _ = collect_source(self._entry(), now - timedelta(days=30), now, 100)
        # The two copies collapse onto the configured host; another subdomain is
        # a different site and keeps its own host.
        assert {h.url for h in hints} == {"https://x.de/presse/a-one", "https://sub.x.de/a-two"}

        entry = {**self._entry(), "url": "https://www.x.de/"}
        assert on_configured_host(hint("https://x.de/a?b=1"), entry).url == "https://www.x.de/a?b=1"

    def test_excluded_dirs_apply_to_every_discovery_method(self, monkeypatch):
        entry = self._entry()
        entry["excluded_dirs"] = ["paid"]
        monkeypatch.setattr("src.collect.pick_accessible_origin",
                            lambda s, u: "https://x.de")
        monkeypatch.setattr(
            "src.collect.collect_from_sitemaps",
            lambda *a, **k: [hint("https://x.de/paid/sitemap-story"),
                             hint("https://x.de/news/sitemap-story")])
        monkeypatch.setattr(
            "src.collect.collect_from_feeds",
            lambda *a, **k: [hint("https://x.de/paid/feed-story", source="rss"),
                             hint("https://x.de/news/feed-story", source="rss")])
        now = datetime.now(tz=BERLIN_TZ)
        hints, error = collect_source(entry, now - timedelta(days=30), now, 100)
        assert error is None
        assert {h.url for h in hints} == {
            "https://x.de/news/sitemap-story", "https://x.de/news/feed-story"}

    def test_source_specific_url_fragments_exclude_indexes_not_sibling_news(self):
        entry = self._entry()
        entry["excluded_url_substrings"] = ["newsuebersicht"]
        assert url_is_excluded(
            "https://x.de/news/acme-newsuebersicht-123.html", entry)
        assert not url_is_excluded(
            "https://x.de/news/acme-eroeffnet-paketzentrum-124.html", entry)

    @pytest.mark.parametrize("pattern,excluded,kept", [
        ("^/magazin(/[a-z]+)?/?$",
         ["https://www.etailment.de/magazin", "https://www.etailment.de/magazin/ki",
          "https://www.etailment.de/magazin/nachhaltigkeit/"],
         ["https://www.etailment.de/magazin/"
          "jd-com-stoesst-bei-mediamarktsaturn-an-europas-pruefgrenze-im-handel",
          "https://www.etailment.de/magazin/2026-09-14-temu-verliert-den-preisvorteil"]),
        ("^/nachrichten(/[a-z-]+)?/?$",
         ["https://www.verkehrsrundschau.de/nachrichten",
          "https://www.verkehrsrundschau.de/nachrichten/recht-geld"],
         ["https://www.verkehrsrundschau.de/nachrichten/recht-geld/"
          "einfuhrumsatzsteuer-logistikbranche-fordert-tempo-3897413"]),
    ])
    def test_url_patterns_exclude_section_indexes_not_articles(self, pattern, excluded, kept):
        entry = {**self._entry(), "excluded_url_patterns": [pattern]}
        assert all(url_is_excluded(url, entry) for url in excluded)
        assert not any(url_is_excluded(url, entry) for url in kept)
        assert not any(url_is_excluded(url, self._entry()) for url in excluded)

    def test_url_fragments_see_the_query_string(self):
        """BPEX pagination and PDF copies share the bare section path with real
        items one level down; only the query separates them."""
        entry = self._entry()
        entry["excluded_url_substrings"] = ["/aktuelles?"]
        assert url_is_excluded("https://x.de/aktuelles?page_a12=2", entry)
        assert url_is_excluded(
            "https://x.de/aktuelles?file=files%2Fbiek%2FPM_KEP-Studie.pdf", entry)
        assert not url_is_excluded(
            "https://x.de/aktuelles/meldung/interview-markttrends-2026", entry)

    def test_source_specific_title_fragments_catch_retitled_indexes(self):
        entry = self._entry()
        entry["excluded_title_substrings"] = ["Newsübersicht"]
        url = "https://x.de/news/personalie-michael-loeckener-139966.html"
        assert url_is_excluded(url, entry, "Trans-o-flex: Newsübersicht")
        assert not url_is_excluded(url, entry, "Michael Löckener steigt auf")


class TestSlug:
    def test_strips_www(self):
        assert slug_for({"url": "https://www.zeit.de/"}) == "zeit.de"

    def test_keeps_meaningful_subdomains(self):
        assert slug_for({"url": "https://ohn.haendlerbund.de/"}) == "ohn.haendlerbund.de"


class TestFurniture:
    """Index pages list articles instead of being one, so they are never stored."""

    @pytest.mark.parametrize("url", [
        "https://x.de/tag/eu",
        "https://x.de/themen/e-commerce",
        "https://x.de/autoren/jane-doe",
        "https://x.de/thema/Nahost",
        "https://x.de/autoren",
        "https://x.de/",
    ])
    def test_index_pages_are_furniture(self, url):
        assert is_furniture(url)

    @pytest.mark.parametrize("url", [
        # BVL nests its listings one level down, under the blog. Matching only
        # the first segment kept 898 of these while claiming to drop them.
        "https://www.bvl.de/blog/tag/zoll/",
        "https://www.bvl.de/blog/author/dirk-gruninger/",
        "https://www.bvl.de/blog/category/bildung-qualification/",
        "https://www.bvl.de/blog/tag",
    ])
    def test_markers_one_level_down_are_furniture(self, url):
        assert is_furniture(url)

    @pytest.mark.parametrize("url", [
        "https://www.bvl.de/blog/page/2/",
        "https://x.de/news/seite/7",
    ])
    def test_pagination_is_furniture(self, url):
        assert is_furniture(url)

    def test_a_section_called_page_is_not_pagination(self):
        """Only a trailing number makes /page a listing."""
        assert not is_furniture("https://x.de/page/impressum")

    def test_cms_assets_are_furniture(self):
        assert is_furniture("https://www.bvl.de/blog/wp-content/uploads/2024/x.pdf")

    def test_comment_permalinks_are_furniture(self):
        """WordPress reply links are the same post under another URL."""
        assert is_furniture("https://www.bvl.de/blog/a-real-post/?replytocom=47375")

    @pytest.mark.parametrize("url", [
        # Depth is the discriminator: an article nested under a topic keeps its
        # marker segment, and FAZ files author profiles below /feuilleton/buecher/.
        "https://x.de/themen/marktplaetze/ebay-aendert-die-auszahlung",
        "https://x.de/aktuell/feuilleton/buecher/autoren/nachruf-auf-x.html",
        "https://x.de/magazin/temu-verliert-den-preisvorteil",
        # A marker deep in the path is a section name, not a listing. FAZ files
        # 21 real articles under .../buecher/autoren/<slug>; searching every
        # segment instead of the first two drops all of them.
        "https://www.faz.net/aktuell/feuilleton/buecher/autoren/isabel-allende-ueber-ihr-schreiben",
        "https://www.bvl.de/blog/recruiting-wird-digitaler",
        "https://www.bvl.de/presse/meldungen/neuer-vorstand-2026",
    ])
    def test_articles_are_not_furniture(self, url):
        assert not is_furniture(url)


class TestMalformedUrls:
    """BVL's sitemap appends a JavaScript fragment to 816 of its URLs."""

    @pytest.mark.parametrize("url", [
        'https://www.bvl.de/blog/tag/zoll/"%20+%20$(%20img%20)%20.%20attr(',
        'https://www.bvl.de/blog/a-post/"%20+%20$(%20img%20)',
        "https://x.de/a/<script>",
    ])
    def test_broken_template_urls_are_malformed(self, url):
        assert is_malformed(url)

    @pytest.mark.parametrize("url", [
        "https://x.de/magazin/a-real-article",
        # Percent-encoding is normal and must survive unquoting.
        "https://www.bvl.de/blog/shippings-escape-from-the-fossil-trap%ef%bf%bc",
        "https://x.de/news/artikel?id=7&ref=rss",
    ])
    def test_ordinary_urls_are_not_malformed(self, url):
        assert not is_malformed(url)

    def test_store_hints_drops_them(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            n = store_hints(conn, rid, "bvl.de", [
                hint("https://www.bvl.de/blog/a-real-post"),
                hint('https://www.bvl.de/blog/tag/zoll/"%20+%20$(%20img%20)'),
            ])
        assert n == 1

    def test_store_hints_drops_them(self, db):
        with session(db) as conn:
            rid = start_run(conn, "news", "a", "b")
            n = store_hints(conn, rid, "x.de", [hint("https://x.de/magazin/a-real-one"),
                                                hint("https://x.de/tag/eu")])
            urls = [r[0] for r in conn.execute("SELECT url FROM raw_item")]
        assert n == 1
        assert urls == ["https://x.de/magazin/a-real-one"]


class TestNewsSitemapPriority:
    """A news sitemap outranks archive sitemaps when max_per_source is bounded.

    etailment lists news-sitemap.xml last, behind six archive files that share one
    lastmod, so in file order the cap was spent before reaching it — which is why
    2,000 stored rows carried no title and no real date.
    """

    @pytest.mark.parametrize("url,expected", [
        ("https://x.de/news-sitemap.xml", True),
        ("https://x.de/sitemap-news.xml", True),
        ("https://x.de/sitemap/1.xml", False),
        # The filename decides, not the host: every sitemap on a news subdomain
        # would otherwise rank as a news sitemap.
        ("https://news.x.de/sitemap.xml", False),
    ])
    def test_identifies_news_sitemaps_by_filename(self, url, expected):
        assert _is_news_sitemap(url) is expected

    def test_news_sitemaps_sort_ahead_of_equally_dated_archives(self):
        when = datetime(2026, 9, 9, tzinfo=BERLIN_TZ)
        nested = [("https://x.de/sitemap/0.xml", when),
                  ("https://x.de/sitemap/1.xml", when),
                  ("https://x.de/news-sitemap.xml", when)]
        ordered = sorted(nested, key=lambda x: (_is_news_sitemap(x[0]), x[1]), reverse=True)
        assert ordered[0][0] == "https://x.de/news-sitemap.xml"

    def test_news_root_is_traversed_before_general_root(self, monkeypatch):
        from vendor.newscrawler import crawler

        when = datetime(2026, 9, 10, tzinfo=BERLIN_TZ)
        fetched = []

        monkeypatch.setattr(crawler.sources, "get_site_rules", lambda url: {})
        monkeypatch.setattr(
            crawler, "discover_sitemaps",
            lambda session, site_url, extra=None: [
                "https://x.de/sitemap.xml",
                "https://x.de/news-sitemap.xml",
            ],
        )

        def fake_fetch(session, url):
            fetched.append(url)
            return ([(f"https://x.de/{len(fetched)}", when, url, "news_sitemap")], [])

        monkeypatch.setattr(crawler, "fetch_sitemap_urls", fake_fetch)
        hints = crawler.collect_from_sitemaps(
            None, "https://x.de/", when - timedelta(days=1),
            when + timedelta(days=1), max_per_source=1,
        )

        assert fetched == ["https://x.de/news-sitemap.xml"]
        assert hints[0].title == "https://x.de/news-sitemap.xml"

    def test_a_url_listed_by_several_sitemaps_counts_once_against_the_cap(self, monkeypatch):
        """WELT lists 111 URLs as 307 entries in two hours; counting entries spent
        the cap on repeats before the traversal reached some new URLs."""
        from vendor.newscrawler import crawler

        when = datetime(2026, 9, 10, tzinfo=BERLIN_TZ)
        monkeypatch.setattr(crawler.sources, "get_site_rules", lambda url: {})
        monkeypatch.setattr(crawler, "discover_sitemaps", lambda session, site_url, extra=None: [
            "https://x.de/news-sitemap.xml", "https://x.de/sitemap-a.xml",
            "https://x.de/sitemap-b.xml"])
        listings = {
            "https://x.de/news-sitemap.xml": [("https://x.de/one", when, None, "news_sitemap")],
            "https://x.de/sitemap-a.xml": [("https://x.de/one", when, "One", "news_sitemap")],
            "https://x.de/sitemap-b.xml": [("https://x.de/one", when, None, "lastmod"),
                                           ("https://x.de/two", when, None, "lastmod")],
        }
        monkeypatch.setattr(crawler, "fetch_sitemap_urls",
                            lambda session, url: (listings[url], []))

        hints = crawler.collect_from_sitemaps(
            None, "https://x.de/", when - timedelta(days=1), when + timedelta(days=1),
            max_per_source=2)

        assert [(h.url, h.title) for h in hints] == [
            ("https://x.de/one", "One"), ("https://x.de/two", None)]


class TestSitemapCaps:
    """A capped traversal stays ok but says it stopped early."""

    WHEN = datetime(2026, 9, 10, tzinfo=BERLIN_TZ)

    def _collect(self, monkeypatch, roots, listings, max_per_source):
        from vendor.newscrawler import crawler

        monkeypatch.setattr(crawler.sources, "get_site_rules", lambda url: {})
        monkeypatch.setattr(crawler, "discover_sitemaps",
                            lambda session, site_url, extra=None: roots)
        monkeypatch.setattr(crawler, "fetch_sitemap_urls",
                            lambda session, url: listings[url])
        report = {}
        hints = crawler.collect_from_sitemaps(
            None, "https://x.de/", self.WHEN - timedelta(days=1),
            self.WHEN + timedelta(days=1), max_per_source=max_per_source, report=report)
        return hints, report

    def _urls(self, *paths):
        return [(f"https://x.de/{p}", self.WHEN, None, "lastmod") for p in paths]

    def test_url_cap(self, monkeypatch):
        hints, report = self._collect(
            monkeypatch, ["https://x.de/sitemap.xml"],
            {"https://x.de/sitemap.xml": (self._urls("a", "b", "c"), [])}, 2)
        assert len(hints) == 2
        assert report == {"cap": "url_cap", "note": "url cap 2 reached, 0 sitemaps unread"}

    def test_fetch_cap(self, monkeypatch):
        children = [(f"https://x.de/sitemap-{i}.xml", None) for i in range(101)]
        listings = {"https://x.de/sitemap.xml": ([], children)}
        listings.update({url: (self._urls(f"a-{i}"), []) for i, (url, _) in enumerate(children)})
        hints, report = self._collect(monkeypatch, ["https://x.de/sitemap.xml"], listings, 2000)
        assert len(hints) == 99
        assert report == {"cap": "fetch_cap", "note": "fetch cap 100 reached, 2 sitemaps unread"}

    def test_normal_tree_reports_nothing(self, monkeypatch):
        hints, report = self._collect(
            monkeypatch, ["https://x.de/sitemap.xml"],
            {"https://x.de/sitemap.xml": (self._urls("a", "b"), [])}, 2)
        assert len(hints) == 2 and report == {}

    def test_collection_keeps_a_truncated_source_ok_and_notes_it(self, monkeypatch, tmp_path):
        from src.collect import run_collection

        source_file = tmp_path / "sources.json"
        source_file.write_text(json.dumps([{"url": "https://x.de/", "sitemap": True}]),
                               encoding="utf-8")
        db_path = tmp_path / "t.sqlite3"
        migrate(db_path)
        monkeypatch.setattr("src.collect.pick_accessible_origin", lambda s, u: "https://x.de")

        def capped(*_args, report=None, **_kwargs):
            report.update(cap="url_cap", note="url cap 1 reached, 3 sitemaps unread")
            return [hint("https://x.de/a-one")]

        monkeypatch.setattr("src.collect.collect_from_sitemaps", capped)
        summary = run_collection(source_file, db_path=db_path, workers=1, body_limit=1)
        with session(db_path) as conn:
            row = conn.execute("SELECT status, error FROM run_source").fetchone()
        assert summary["failed"] == 0
        assert (row["status"], row["error"]) == (
            "ok", "truncated: url cap 1 reached, 3 sitemaps unread")


class TestExtraSitemapUrls:
    """Known sitemap roots supplement, rather than replace, robots.txt."""

    class Response:
        status_code = 200
        text = "Sitemap: https://www.example.de/sitemap.xml\n"

    class Session:
        def get(self, *args, **kwargs):
            return TestExtraSitemapUrls.Response()

    def test_extra_sitemap_is_merged_with_declared_sitemap(self):
        found = discover_sitemaps(
            self.Session(), "https://www.example.de/",
            ["https://www.example.de/news-sitemap.xml"],
        )
        assert found == [
            "https://www.example.de/sitemap.xml",
            "https://www.example.de/news-sitemap.xml",
        ]

    def test_relative_extra_sitemap_is_resolved_and_duplicates_are_removed(self):
        found = discover_sitemaps(
            self.Session(), "https://www.example.de/",
            ["news-sitemap.xml", "https://example.de/sitemap.xml", ""],
        )
        assert found == [
            "https://www.example.de/sitemap.xml",
            "https://www.example.de/news-sitemap.xml",
        ]


class TestSitemapDateProvenance:
    """<news:publication_date> says when an article appeared; <lastmod> says only
    that the URL changed. Both land in the same hint field, so the hint must say
    which one it carries - a report may print the first and never the second."""

    SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url><loc>https://x.de/news/a</loc><lastmod>2026-09-01T10:00:00+02:00</lastmod>
    <news:news><news:title>A</news:title>
    <news:publication_date>2026-08-30T08:00:00+02:00</news:publication_date></news:news></url>
  <url><loc>https://x.de/b</loc><lastmod>2026-09-01T10:00:00+02:00</lastmod></url>
  <url><loc>https://x.de/c</loc></url>
</urlset>"""

    def test_entries_are_labelled_by_the_field_that_dated_them(self, monkeypatch):
        from vendor.newscrawler import crawler

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/xml"}
            content = self.SITEMAP

            def raise_for_status(self):
                pass

        monkeypatch.setattr(crawler, "polite_get", lambda session, url: Response())
        entries, nested = crawler.fetch_sitemap_urls(None, "https://x.de/sitemap.xml")
        assert nested == []
        assert [(loc, source) for loc, _when, _title, source in entries] == [
            ("https://x.de/news/a", "news_sitemap"),
            ("https://x.de/b", "lastmod"),
            ("https://x.de/c", None),
        ]
        # The news date wins over lastmod when both are present.
        assert entries[0][1].date().isoformat() == "2026-08-30"
        assert entries[0][2] == "A"
