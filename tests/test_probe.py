"""Tests for the probe's pure reporting logic.

Network behaviour is not covered here; these guard the parts that turn collected
hints into the numbers a source entry gets written from.
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.probe import (  # noqa: E402
    ATTEMPTS,
    FileRecorder,
    MethodResult,
    _dir_key,
    _looks_like_article,
    _run_method,
    directory_table,
    draft_entry,
    file_yield,
    pin_suggestion,
    suggest_dirs,
    token_hints,
    unreachable_feeds,
)
from vendor.newscrawler.crawler import BERLIN_TZ, ArticleHint  # noqa: E402


def hint(url, source="sitemap"):
    return ArticleHint(url=url, published_at=None, title=None, source=source)


class TestDirKey:
    def test_takes_leading_segments(self):
        url = "https://www.zeit.de/politik/deutschland/2026-09/wahl-artikel"
        assert _dir_key(url, 1) == "politik"
        assert _dir_key(url, 2) == "politik/deutschland"

    def test_root_url_has_no_prefix(self):
        assert _dir_key("https://www.zeit.de/", 1) == "(root)"

    def test_depth_beyond_path_returns_what_exists(self):
        assert _dir_key("https://www.zeit.de/politik/", 3) == "politik"


class TestLooksLikeArticle:
    @pytest.mark.parametrize("url", [
        "https://www.zeit.de/politik/deutschland/2026-09/wahl-manipulation-afd",
        "https://www.spiegel.de/politik/ein-artikel-a-12345.html",
    ])
    def test_accepts_slugged_articles(self, url):
        assert _looks_like_article(url)

    @pytest.mark.parametrize("url", [
        "https://www.zeit.de/",
        "https://www.zeit.de/politik",
        "https://www.zeit.de/politik/",
        "https://www.e-commerce-magazin.de/e-commerce",
    ])
    def test_rejects_section_indexes(self, url):
        assert not _looks_like_article(url)

    def test_accepts_flat_permalinks(self):
        """Sites without a section prefix still publish articles."""
        assert _looks_like_article(
            "https://www.e-commerce-magazin.de/temu-und-shein-unter-druck-neue-eu-regeln"
        )


class TestDirectoryTable:
    def test_counts_per_method_and_orders_by_volume(self):
        results = [
            MethodResult("sitemap", hints=[
                hint("https://x.de/politik/a-one"),
                hint("https://x.de/politik/a-two"),
                hint("https://x.de/sport/b-one"),
            ]),
            MethodResult("feeds", hints=[hint("https://x.de/politik/a-three", "rss")]),
        ]
        order, per_dir = directory_table(results, depth=1)
        assert order == ["politik", "sport"]
        assert per_dir["politik"] == Counter({"sitemap": 2, "feeds": 1})

    def test_excludes_section_indexes(self):
        results = [MethodResult("sitemap", hints=[hint("https://x.de/politik")])]
        order, _ = directory_table(results, depth=1)
        assert order == []

    def test_same_url_from_two_methods_counts_once_each(self):
        url = "https://x.de/politik/a-one"
        results = [
            MethodResult("sitemap", hints=[hint(url), hint(url)]),
            MethodResult("feeds", hints=[hint(url, "rss")]),
        ]
        _, per_dir = directory_table(results, depth=1)
        assert per_dir["politik"] == Counter({"sitemap": 1, "feeds": 1})


class TestSuggestDirs:
    def test_covers_the_bulk_and_drops_the_tail(self):
        per_dir = {
            "politik": Counter({"sitemap": 80}),
            "wirtschaft": Counter({"sitemap": 15}),
            "sport": Counter({"sitemap": 3}),
            "reisen": Counter({"sitemap": 2}),
        }
        order = ["politik", "wirtschaft", "sport", "reisen"]
        assert suggest_dirs(order, per_dir) == ["politik", "wirtschaft"]

    def test_caps_at_ten_prefixes(self):
        per_dir = {f"d{i}": Counter({"sitemap": 2}) for i in range(30)}
        order = list(per_dir)
        assert len(suggest_dirs(order, per_dir)) == 10

    def test_no_articles_yields_no_dirs(self):
        assert suggest_dirs([], {}) == []

    def test_flat_permalinks_yield_no_dirs(self):
        """Every prefix seen once means slugs, not sections - suggest nothing."""
        per_dir = {f"some-article-title-{i}": Counter({"feeds": 1}) for i in range(20)}
        assert suggest_dirs(list(per_dir), per_dir) == []

    def test_ignores_root(self):
        per_dir = {"(root)": Counter({"sitemap": 99}), "politik": Counter({"sitemap": 4})}
        assert suggest_dirs(["(root)", "politik"], per_dir) == ["politik"]


class TestRunMethod:
    @pytest.fixture(autouse=True)
    def no_retry_pause(self, monkeypatch):
        """The pause is politeness toward the site, not behaviour under test."""
        monkeypatch.setattr("src.probe.RETRY_PAUSE_SECONDS", 0)

    def test_retries_an_empty_result_then_succeeds(self):
        calls = []

        def collect():
            calls.append(1)
            return [] if len(calls) < 2 else [hint("https://x.de/politik/a-one")]

        result = _run_method("feeds", collect)
        assert result.ok and result.attempts == 2

    def test_gives_up_after_all_attempts(self):
        result = _run_method("feeds", lambda: [])
        assert not result.ok
        assert result.status == "empty"
        assert result.attempts == ATTEMPTS

    def test_an_exception_is_reported_and_not_retried(self):
        calls = []

        def collect():
            calls.append(1)
            raise ConnectionError("refused")

        result = _run_method("sitemap", collect)
        assert result.status == "ERROR"
        assert "ConnectionError: refused" in result.error
        assert len(calls) == 1

    def test_capped_when_the_collector_fills_its_limit(self):
        hints = [hint(f"https://x.de/politik/a-{i}") for i in range(5)]
        assert _run_method("sitemap", lambda: hints, cap=5).capped
        assert not _run_method("sitemap", lambda: hints, cap=6).capped


class TestSourceRules:
    """get_site_rules is what every discovery method reads its config from."""

    def _load(self, tmp_path, entries):
        from vendor.newscrawler.source_loader import sources
        path = tmp_path / "sources.json"
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        sources.clear_cache()
        sources.load_sources(str(path))
        return sources

    def test_feed_urls_reach_the_rules(self, tmp_path):
        s = self._load(tmp_path, [{
            "url": "https://example.de/", "feeds": True,
            "feed_urls": ["https://example.de/odd/path/rss_en"],
        }])
        rules = s.get_site_rules("https://example.de/some/article-here")
        assert rules["feed_urls"] == ["https://example.de/odd/path/rss_en"]

    def test_feed_urls_default_empty(self, tmp_path):
        s = self._load(tmp_path, [{"url": "https://example.de/", "feeds": True}])
        assert s.get_site_rules("https://example.de/")["feed_urls"] == []

    def test_extra_sitemap_urls_reach_the_rules(self, tmp_path):
        s = self._load(tmp_path, [{
            "url": "https://example.de/", "sitemap": True,
            "extra_sitemap_urls": ["https://example.de/news-sitemap.xml"],
        }])
        rules = s.get_site_rules("https://example.de/some/article-here")
        assert rules["extra_sitemap_urls"] == [
            "https://example.de/news-sitemap.xml",
        ]

    def test_extra_sitemap_urls_default_empty(self, tmp_path):
        s = self._load(tmp_path, [{"url": "https://example.de/", "sitemap": True}])
        assert s.get_site_rules("https://example.de/")["extra_sitemap_urls"] == []

    def test_unknown_domain_has_no_rules(self, tmp_path):
        s = self._load(tmp_path, [{"url": "https://example.de/"}])
        assert s.get_site_rules("https://other.de/") is None


class TestUnreachableFeeds:
    def test_flags_feeds_outside_the_known_paths(self):
        declared = [
            "https://www.tagesschau.de/index~rss2.xml",
            "https://www.tagesschau.de/index~atom.xml",
        ]
        assert unreachable_feeds(declared) == declared

    def test_ignores_feeds_the_crawler_already_tries(self):
        assert unreachable_feeds(["https://x.de/rss.xml", "https://x.de/feed"]) == []

    def test_trailing_slash_does_not_make_a_feed_look_new(self):
        assert unreachable_feeds(["https://x.de/feed/"]) == []

    def test_nothing_declared_means_nothing_missed(self):
        assert unreachable_feeds([]) == []


class TestDraftEntry:
    def _report(self, sitemap_ok, feeds_ok, frontpage_ok):
        return {
            "origin": "https://www.zeit.de",
            "domain": "www.zeit.de",
            "results": [
                MethodResult("sitemap", hints=[hint("https://x.de/a/b-c")] if sitemap_ok else []),
                MethodResult("feeds", hints=[hint("https://x.de/a/b-c")] if feeds_ok else []),
                MethodResult("frontpage", hints=[hint("https://x.de/a/b-c")] if frontpage_ok else []),
            ],
        }

    def test_organization_drops_the_host_prefix(self):
        entry = draft_entry(self._report(True, False, False), [])
        assert entry["organization"] == "Zeit"
        assert entry["url"] == "https://www.zeit.de/"

    def test_frontpage_is_not_suggested_alongside_a_working_sitemap(self):
        entry = draft_entry(self._report(True, False, True), [])
        assert entry["sitemap"] is True
        assert entry["frontpage"] is False

    def test_frontpage_is_suggested_when_the_sitemap_fails(self):
        entry = draft_entry(self._report(False, False, True), [])
        assert entry["frontpage"] is True

    def test_allowed_dirs_are_carried_through(self):
        entry = draft_entry(self._report(True, True, False), ["politik", "wirtschaft"])
        assert entry["allowed_dirs"] == ["politik", "wirtschaft"]


WINDOW_START = datetime(2026, 9, 14, tzinfo=BERLIN_TZ)
WINDOW_END = datetime(2026, 9, 16, 12, 0, tzinfo=BERLIN_TZ)
IN_WINDOW = datetime(2026, 9, 16, 8, 0, tzinfo=BERLIN_TZ)
TOO_OLD = datetime(2026, 3, 1, tzinfo=BERLIN_TZ)


class Recorded:
    """Feeds FileRecorder the way collect_from_sitemaps does."""

    class Response:
        def __init__(self, headers):
            self.headers = headers

    @staticmethod
    def build(files, validators=()):
        recorder = FileRecorder()
        for url, entries in files.items():
            if url in validators:
                recorder.seen(url, Recorded.Response({"ETag": '"abc"'}))
            recorder.remember(url, entries, [])
        return recorder


def sm_entry(loc, when=IN_WINDOW, title=None, date_source="lastmod"):
    return (loc, when, title, date_source)


class TestFileYield:
    """What a sitemap file would really contribute, not how many URLs it lists."""

    def test_only_urls_that_survive_every_collection_filter_are_counted(self):
        recorder = Recorded.build({
            "https://x.de/news.xml": [
                sm_entry("https://x.de/politik/real-article-here"),
                sm_entry("https://x.de/politik/too-old-article", when=TOO_OLD),
                sm_entry("https://x.de/sport/wrong-section-here"),
                sm_entry("https://x.de/politik/blocked-article", ),
            ],
        })
        entry = {"url": "https://x.de/", "allowed_dirs": ["politik"],
                 "excluded_url_substrings": ["blocked"]}
        per_file = file_yield(recorder, entry, WINDOW_START, WINDOW_END)

        assert per_file["https://x.de/news.xml"]["kept"] == \
            {"https://x.de/politik/real-article-here"}
        assert per_file["https://x.de/news.xml"]["entries"] == 4

    def test_news_dates_titles_and_validators_are_reported(self):
        recorder = Recorded.build(
            {"https://x.de/news.xml": [
                sm_entry("https://x.de/one-real-article", title="A",
                         date_source="news_sitemap"),
                sm_entry("https://x.de/two-real-article"),
            ]},
            validators={"https://x.de/news.xml"},
        )
        row = file_yield(recorder, {}, WINDOW_START, WINDOW_END)["https://x.de/news.xml"]
        assert (row["news_dates"], row["titled"], row["validators"]) == (1, 1, True)

    def test_a_file_without_validators_says_so(self):
        recorder = Recorded.build(
            {"https://x.de/news.xml": [sm_entry("https://x.de/one-real-article")]})
        row = file_yield(recorder, {}, WINDOW_START, WINDOW_END)["https://x.de/news.xml"]
        assert row["validators"] is False


class TestPinSuggestion:
    """The point of the probe: which files carry the articles."""

    def test_the_fewest_files_that_cover_every_url_are_chosen(self):
        per_file = {
            "news.xml": {"kept": {"a", "b", "c"}, "news_dates": 3},
            "archive-1.xml": {"kept": {"a"}, "news_dates": 0},
            "archive-2.xml": {"kept": set(), "news_dates": 0},
            "wirtschaft.xml": {"kept": {"d"}, "news_dates": 0},
        }
        chosen, uncovered = pin_suggestion(per_file)
        assert set(chosen) == {"news.xml", "wirtschaft.xml"}
        assert uncovered == set()

    def test_a_news_sitemap_wins_a_tie_against_a_plain_one(self):
        per_file = {
            "plain.xml": {"kept": {"a", "b"}, "news_dates": 0},
            "news.xml": {"kept": {"a", "b"}, "news_dates": 2},
        }
        chosen, _ = pin_suggestion(per_file)
        assert chosen == ["news.xml"]

    def test_nothing_kept_suggests_nothing(self):
        assert pin_suggestion({"a.xml": {"kept": set(), "news_dates": 0}}) == ([], set())


class TestTokenHints:
    """A pin naming this month breaks at the next one unless it carries a token."""

    def test_a_dated_path_is_templated(self):
        [hint_line] = token_hints(
            ["https://www.welt.de/sitemaps/sitemap/2026/09/sitemap.xml.gz"],
            {}, WINDOW_END)
        assert "{YYYY}/{MM}" in hint_line

    def test_a_dated_and_numbered_file_gets_both_tokens(self):
        url = "https://www.spiegel.de/sitemaps/article/sitemap-2026-09_3.xml"
        per_file = {url: {}, "https://www.spiegel.de/sitemaps/article/sitemap-2026-09_1.xml": {}}
        [hint_line] = token_hints([url], per_file, WINDOW_END)
        assert "sitemap-{YYYY}-{MM}_{LATEST}.xml" in hint_line
        assert '"index"' in hint_line

    def test_a_stable_name_gets_no_hint(self):
        assert token_hints(["https://x.de/news-sitemap.xml"], {}, WINDOW_END) == []

    def test_a_number_that_is_not_a_date_is_not_templated_as_one(self):
        """0916 is not this month; only a four-digit year anchors a date."""
        assert token_hints(["https://x.de/sitemap-0916.xml"], {}, WINDOW_END) == []
