"""Tests for collection storage and per-source error handling.

store_hints carries mvp_plan's dedup and versioning guarantees, and collect_source
decides whether one broken discovery method costs a source its other methods. Both
were previously proven only by a manual re-run.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collect import (  # noqa: E402
    collect_source, is_furniture, is_malformed, slug_for, store_hints,
)
from src.db import migrate, session, start_run  # noqa: E402
from vendor.newscrawler.crawler import (  # noqa: E402
    BERLIN_TZ, ArticleHint, _is_news_sitemap,
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
