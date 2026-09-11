"""Page-date extraction pinned to real publisher markup.

The fixtures under tests/fixtures/dates are reduced captures of live pages (see
capture.py there): the head, every JSON-LD block and every <time> element, kept
verbatim. Each expected value below was read off the markup by hand, not taken
from the extractor. A failure here means a publisher changed its template, which
otherwise shows up only as that source quietly falling back to sitemap lastmod.
"""

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bodies import _page_published_at  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dates"


@pytest.mark.parametrize(("fixture", "expected"), [
    # One <time itemprop="datePublished">; nothing in JSON-LD or meta.
    ("bevh_detail", "2026-09-09"),
    # One <time datetime> holding epoch seconds.
    ("dslv_meldung", "2026-09-10T12:39:00+02:00"),
    # JSON-LD datePublished 2015 beside dateModified 2026: published wins.
    ("haendlerbund_article", "2015-06-24T22:00:00+00:00"),
    # JSON-LD NewsArticle with a date-only datePublished, plus one agreeing <time>.
    ("etailment_article", "2026-09-10"),
    # JSON-LD NewsArticle, og:type says "website" - the type tag is not consulted.
    ("verkehrsrundschau_article", "2026-09-10T13:52:00+02:00"),
    # Drupal's German-weekday, US-order string normalised.
    ("logistik_heute_article", "2026-09-10T14:33:00+02:00"),
    # WordPress/Yoast: Article and WebPage both carry datePublished.
    ("wettbewerbszentrale_article", "2026-09-10T05:44:42+00:00"),
    # Several <time> elements naming different days: the article's own plus
    # one per related-news card. Refused rather than guessed; the feed dates it.
    ("edpb_news", None),
    # Class-action record: filed, served, status. None is a publication date.
    ("verbraucherzentrale_verbandsklage", None),
    # A listing with one <time itemprop="datePublished"> per teaser.
    ("bpex_listing", None),
    # Section indexes: no structured date at all, because they are not articles.
    ("etailment_section", None),
    ("verkehrsrundschau_section", None),
])
def test_real_templates_yield_the_expected_date_or_none(fixture, expected):
    markup = (FIXTURES / f"{fixture}.html").read_text(encoding="utf-8")
    assert _page_published_at(BeautifulSoup(markup, "lxml")) == expected
