"""Capture reduced real-page fixtures for the page-date extractor.

Run from the repository root, with network access:

    python tests/fixtures/dates/capture.py

Each fixture keeps only what ``src.bodies._page_published_value`` reads - the
``<head>`` title and ``<meta>`` tags, every JSON-LD script, every ``<time>``
element with its text - plus the ``<h1>`` for orientation. Everything else is
dropped, so a 660 KB page becomes a few KB while the date-bearing markup stays
verbatim, including how many ``<time>`` elements the page carries, which the
lone-``<time>`` rule depends on.

``tests/test_page_dates.py`` pins the expected value for each fixture. When a
publisher changes its template that test fails loudly instead of the source
silently falling back to a sitemap ``lastmod``. Re-run this script, read the new
markup, decide the right value by hand, then update the test. Never update the
expectation from the extractor's own output.
"""

from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.bodies import _page_published_at  # noqa: E402

HERE = Path(__file__).resolve().parent

PAGES = [
    # article pages
    ("bevh_detail",
     "https://bevh.org/detail/verpackungsverordnung-bevh-fordert-abschaffung-der-bevollmaechtigtenpflicht"),
    ("dslv_meldung",
     "https://www.dslv.org/de/aktuelles/meldung/sendungskosten-im-stueckgutmarkt-um-durchschnittlich-47-prozent-gestiegen"),
    ("haendlerbund_article",
     "https://www.haendlerbund.de/de/news/aktuelles/rechtliches/1592-abmahnung-nickelfrei"),
    ("etailment_article",
     "https://www.etailment.de/magazin/haendler-setzen-auf-ki-agenten-ihre-kunden-aber-nicht"),
    ("verkehrsrundschau_article",
     "https://www.verkehrsrundschau.de/nachrichten/transport-logistik/zukunftskongress-logistik-resilienz-staerken-in-unsicheren-zeiten-3901524"),
    ("logistik_heute_article",
     "https://logistik-heute.de/news/seehaefen-arbeitgeber-legen-neues-tarifangebot-fuer-hafenarbeiter-vor-283312.html"),
    ("wettbewerbszentrale_article",
     "https://www.wettbewerbszentrale.de/irrefuehrende-werbeschreiben-im-behoerden-look/"),
    # pages that must NOT yield a date
    ("edpb_news",
     "https://www.edpb.europa.eu/news/health-data-breach-the-cnil-fined-hopital-prive-de-la-loire-500-000-eur_en"),
    ("verbraucherzentrale_verbandsklage",
     "https://www.verbraucherzentrale.de/verbandsklagen/klage-gegen-yd-yourdelivery-gmbh-105076"),
    ("bpex_listing", "https://bpex-ev.de/aktuelles?page_a12=2"),
    ("etailment_section", "https://www.etailment.de/magazin/logistik"),
    ("verkehrsrundschau_section", "https://www.verkehrsrundschau.de/nachrichten/recht-geld"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
}


def reduce(markup: bytes) -> BeautifulSoup:
    source = BeautifulSoup(markup, "lxml")
    out = BeautifulSoup("<html><head></head><body></body></html>", "lxml")
    head, body = out.head, out.body
    if source.title:
        head.append(copy.copy(source.title))
    for meta in source.find_all("meta"):
        head.append(copy.copy(meta))
    for script in source.find_all("script", type="application/ld+json"):
        head.append(copy.copy(script))
    heading = source.find("h1")
    if heading:
        body.append(copy.copy(heading))
    for tag in source.find_all("time"):
        body.append(copy.copy(tag))
    return out


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)
    for name, url in PAGES:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        reduced = reduce(response.content)
        path = HERE / f"{name}.html"
        path.write_text(
            f"<!-- captured {date.today().isoformat()} from {url}\n"
            "     reduced to the markup src.bodies._page_published_value reads; "
            "see capture.py -->\n" + reduced.prettify(), encoding="utf-8")
        print(f"{name:36} {len(response.content):>8} -> {path.stat().st_size:>6} bytes  "
              f"time={len(reduced.find_all('time'))}  "
              f"extractor={_page_published_at(reduced)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
