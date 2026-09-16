"""The drift check behind pinning: what a full walk finds that the pins do not."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.rediscover as rediscover  # noqa: E402
from src.discovery import PinnedSitemapError  # noqa: E402
from vendor.newscrawler.crawler import ArticleHint, BERLIN_TZ  # noqa: E402

END = datetime(2026, 9, 16, 12, 0, tzinfo=BERLIN_TZ)
SINCE = END - timedelta(days=2)
WHEN = datetime(2026, 9, 16, 8, 0, tzinfo=BERLIN_TZ)

ENTRY = {"url": "https://x.de/", "organization": "X", "sitemap": True,
         "sitemap_urls": ["https://x.de/news.xml"]}


def hint(url):
    return ArticleHint(url=url, published_at=WHEN, title=None, source="sitemap",
                       date_source="lastmod")


@pytest.fixture
def fake(monkeypatch):
    """Install a pinned result and a walk result, in the shapes the real ones have."""

    def install(pinned, walked, pin_error=None):
        def pins(entry, since, end, *, session, cache=None):
            if pin_error:
                raise pin_error
            return [hint(url) for url in pinned]

        def walk(session, site_url, since, end, *, max_per_source, cache):
            for file_url, locs in walked.items():
                cache.remember(file_url,
                               [(loc, WHEN, None, "lastmod") for loc in locs], [])
            return []

        monkeypatch.setattr(rediscover, "collect_from_pinned", pins)
        monkeypatch.setattr(rediscover, "collect_from_sitemaps", walk)
        monkeypatch.setattr(rediscover, "_session", lambda: None)

    return install


def test_pins_that_cover_the_walk_report_nothing(fake):
    fake(pinned=["https://x.de/one-real-article", "https://x.de/two-real-article"],
         walked={"https://x.de/news.xml": ["https://x.de/one-real-article",
                                           "https://x.de/two-real-article"]})
    result = rediscover.check_source(ENTRY, SINCE, END)

    assert result["missing"] == {}
    assert (result["pinned"], result["walked"], result["files"]) == (2, 2, 1)


def test_a_file_the_pins_do_not_cover_is_named_with_its_urls(fake):
    fake(pinned=["https://x.de/one-real-article"],
         walked={"https://x.de/news.xml": ["https://x.de/one-real-article"],
                 "https://x.de/wirtschaft.xml": ["https://x.de/three-real-article"]})
    result = rediscover.check_source(ENTRY, SINCE, END)

    assert result["missing"] == {
        "https://x.de/wirtschaft.xml": ["https://x.de/three-real-article"]}
    assert "only in https://x.de/wirtschaft.xml" in "\n".join(
        rediscover.format_result(result))


def test_a_url_only_the_pins_found_is_counted_not_reported(fake):
    """A news sitemap robots.txt does not declare is often why it was pinned."""
    fake(pinned=["https://x.de/one-real-article", "https://x.de/two-real-article"],
         walked={"https://x.de/news.xml": ["https://x.de/one-real-article"]})
    result = rediscover.check_source(ENTRY, SINCE, END)

    assert result["missing"] == {}
    assert result["only_pinned"] == 1


def test_furniture_and_excluded_urls_are_not_called_drift(fake):
    """Both sides must count what collection counts, or every run reports noise."""
    fake(pinned=["https://x.de/one-real-article"],
         walked={"https://x.de/news.xml": ["https://x.de/one-real-article",
                                           "https://x.de/tag/logistik",
                                           "https://x.de/blocked-real-article"]})
    entry = dict(ENTRY, excluded_url_substrings=["blocked"])
    result = rediscover.check_source(entry, SINCE, END)

    assert result["missing"] == {}


def test_a_broken_pin_is_reported_and_the_walk_still_runs(fake):
    fake(pinned=[], walked={"https://x.de/news.xml": ["https://x.de/one-real-article"]},
         pin_error=PinnedSitemapError("pinned sitemap unavailable: https://x.de/news.xml"))
    result = rediscover.check_source(ENTRY, SINCE, END)

    assert "pinned sitemap unavailable" in result["error"]
    assert result["missing"] == {
        "https://x.de/news.xml": ["https://x.de/one-real-article"]}
