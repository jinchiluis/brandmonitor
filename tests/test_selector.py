"""Tests for the deterministic, read-only news candidate selector."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.news_selector import (pdf_filename_text, select_candidate, slug_text,
                               selection_from_db, url_metadata)  # noqa: E402
from src.profile import load_profile  # noqa: E402

# The real shipped profile, so these tests fail if it stops matching what the
# client actually asked for.
PROFILE = load_profile("jt-express")


def select(title, url, body=None):
    return select_candidate(title, url, PROFILE, body)


@pytest.mark.parametrize("title", [
    "Temu gerät unter Druck",
    "SHEIN faces a new investigation",
    "Ali Express muss reagieren",
    "TikTok Shop startet in Deutschland",
    "J&T Express expands in Europe",
    "极兔进入欧洲市场",
])
def test_direct_entities_trigger(title):
    reasons, fields = select(title, "https://example.test/story")
    assert reasons
    assert fields == ("title",)


@pytest.mark.parametrize("title", [
    "EU schafft Zollfreigrenze ab",
    "Reform of low-value consignments agreed",
    "Neue Regeln durch den Digital Services Act",
    "GPSR wird verschärft",
])
def test_strong_topics_trigger(title):
    reasons, _ = select(title, "https://example.test/story")
    assert any(reason.startswith("topic:") for reason in reasons)


@pytest.mark.parametrize("title", [
    "China plant neue Raumstation",
    "Pakete kommen später an",
    "Neue Plattform für Musik",
    "Onlineversand wird immer beliebter",
    "Zoll beschlagnahmt Kokain",
    "Neue Regeln für den Import",
    "Debatte über die Lieferkette",
    "Wachstum im KEP-Markt",
])
def test_broad_keywords_trigger_independently(title):
    reasons, fields = select(title, "https://example.test/story")
    assert any(reason.startswith("keyword:") for reason in reasons)
    assert fields == ("title",)


def test_import_does_not_match_important():
    reasons, fields = select("An important announcement", "https://example.test/story")
    assert reasons == ()
    assert fields == ()


@pytest.mark.parametrize("title", [
    "Neue Regeln für chinesische Onlinehändler",
    "Zoll prüft Pakete aus China strenger",
    "EU reguliert den grenzüberschreitenden Onlineversand",
    "Haftung von Online-Marktplätzen wird verschärft",
])
def test_sector_phrasings_trigger_on_a_keyword(title):
    reasons, _ = select(title, "https://example.test/story")
    assert any(reason.startswith("keyword:") for reason in reasons)


def test_url_slug_is_used_but_hostname_is_not():
    reasons, fields = select(None, "https://temu.example.test/news/eu-zoll-trifft-temu-und-shein")
    assert "brand:Temu" in reasons
    assert "brand:Shein" in reasons
    assert fields == ("slug",)

    reasons, fields = select(None, "https://temu.example.test/news/ordinary-story")
    assert reasons == ()
    assert fields == ()


def test_encoded_j_and_t_slug_matches():
    reasons, fields = select(None, "https://example.test/j%26t-express-expands")
    assert "brand:J&T" in reasons
    assert fields == ("slug",)


def test_slug_text_is_human_readable():
    assert slug_text("https://example.test/a/temu-zoll-wirkt") == "temu zoll wirkt"


def test_pdf_filename_in_query_is_used_before_generic_path():
    url = ("https://bpex-ev.de/aktuelles?file=files%2Fpresse%2F"
           "PM_BPEX_KEP-Studie_2026.pdf")
    assert pdf_filename_text(url) == "PM BPEX KEP Studie 2026"
    assert url_metadata(url) == ("PM BPEX KEP Studie 2026", "pdf_filename")
    reasons, fields = select(None, url)
    assert "keyword:KEP" in reasons
    assert fields == ("pdf_filename",)


def test_numeric_final_id_falls_back_to_previous_meaningful_path_segment():
    url = ("https://www.handelsblatt.com/politik/"
           "zoll-reform-fuer-pakete-aus-china/100246972.html")
    assert slug_text(url) == "zoll reform fuer pakete aus china"
    reasons, fields = select(None, url)
    assert "keyword:China/chinesisch" in reasons
    assert fields == ("slug",)


@pytest.mark.parametrize("url", [
    "https://example.test/news/",
    "https://example.test/de/aktuelles/meldung",
    "https://example.test/blog/734/",
])
def test_generic_or_opaque_paths_have_no_metadata_label(url):
    assert slug_text(url) == ""


def test_db_selection_preserves_discovery_title_and_supports_all_kinds(tmp_path):
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)"
    )
    rows = [
        # A later bare sitemap hint must not erase this discovery title.
        ("news.test", "news", "n1", 1, "https://news.test/100.html", "Temu News",
         None, "2026-09-01", '{"discovered_via":"feed"}'),
        ("news.test", "news", "n1", 2, "https://news.test/100.html", None,
         None, "2026-09-02", '{"discovered_via":"sitemap"}'),
        # Generic routing is reported as unknown rather than silently rejected.
        ("news.test", "news", "n2", 1, "https://news.test/news/", None,
         None, "2026-09-02", '{"discovered_via":"feed"}'),
        # Regulatory source selection uses the same URL metadata rules.
        ("reg.test", "regulatory", "r1", 1,
         "https://reg.test/customs-reform-for-parcels", None,
         None, "2026-09-02", '{"discovered_via":"feed"}'),
    ]
    conn.executemany("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    news_sources = tmp_path / "news.json"
    regulatory_sources = tmp_path / "regulatory.json"
    news_sources.write_text(json.dumps([
        {"url": "https://news.test/", "content_mode": "full_text"}
    ]), encoding="utf-8")
    regulatory_sources.write_text(json.dumps([
        {"url": "https://reg.test/", "content_mode": "full_text"}
    ]), encoding="utf-8")

    result = selection_from_db(
        db, ((news_sources, "news"), (regulatory_sources, "regulatory")), "all",
        PROFILE)

    assert result.eligible == 3
    assert {item.source_slug for item in result.candidates} == {"news.test", "reg.test"}
    assert next(item for item in result.candidates
                if item.source_slug == "news.test").title == "Temu News"
    assert len(result.unknowns) == 1
    assert result.unknowns[0].url == "https://news.test/news/"


# ── the client profile is data, not code ─────────────────────────────────

def test_profile_drives_matching_not_the_module():
    """Swapping the profile changes what is selected, with no code change."""
    from src.profile import ClientProfile, Rule
    import re

    other = ClientProfile("acme", "Acme", "v1", (
        Rule("brand", "Acme", re.compile(r"(?<!\w)acme(?!\w)", re.IGNORECASE)),))
    assert select_candidate("Acme expands", "https://x.test/s", other)[0] == ("brand:Acme",)
    assert select_candidate("Temu expands", "https://x.test/s", other)[0] == ()


def test_body_is_matched_when_supplied():
    """The whole point: a brand in paragraph twelve, absent from the headline."""
    title = "Neue Regeln für den Versandhandel"
    url = "https://example.test/news/neue-regeln-versandhandel"
    assert "brand:Temu" not in select(title, url)[0]

    body = ("Die Kommission hat neue Vorgaben beschlossen. " * 5 +
            "Betroffen ist unter anderem Temu, das seine Prozesse anpassen muss.")
    reasons, fields = select(title, url, body)
    assert "brand:Temu" in reasons
    assert "body" in fields


def test_body_matching_can_be_switched_off_for_measurement(tmp_path):
    """--no-bodies reproduces title/slug-only selection, which is how the
    recall cost of the title_only tier gets measured."""
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)")
    payload = json.dumps({"discovered_via": "feed",
                          "body_text": "Ausführlich geht es um Temu und den Zoll."})
    conn.execute("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)",
                 ("news.test", "news", "n1", 1, "https://news.test/neue-regeln-fuer-alle",
                  "Neue Regeln", None, "2026-09-02", payload))
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([
        {"url": "https://news.test/", "content_mode": "full_text"}]), encoding="utf-8")

    with_bodies = selection_from_db(db, ((sources, "news"),), "all", PROFILE)
    assert len(with_bodies.candidates) == 1
    assert "body" in with_bodies.candidates[0].matched_in

    without = selection_from_db(db, ((sources, "news"),), "all", PROFILE,
                                use_bodies=False)
    assert without.candidates == ()


# ── the body is held to a higher bar than the title ──────────────────────

def test_one_broad_keyword_in_a_body_is_not_enough():
    """Measured: a single keyword in a body selected 116 items, almost all noise.

    German homonyms are the sharpest case - "13-Zoll-Display" is inches, not
    customs, and "Internet und Router im Paket" is a bundle, not a parcel.
    """
    ipad = ("Das Gerät kommt wahlweise mit 11- oder 13-Zoll-Display und viel "
            "Speicher. " * 8)
    reasons, fields = select("Für wen sich das iPad Air lohnt",
                             "https://t3n.test/news/ipad-air-angebot", ipad)
    assert reasons == ()
    assert "body" not in fields


def test_two_broad_keywords_in_a_body_are_enough():
    body = ("Die neue Verordnung betrifft den Onlinehandel und verpflichtet "
            "Marktplätze zu strengeren Prüfungen. " * 6)
    reasons, fields = select("Neue Verordnung", "https://x.test/news/neue-verordnung", body)
    assert len(reasons) >= 2
    assert "body" in fields


def test_a_brand_in_a_body_always_counts_alone():
    """Finding a brand in paragraph twelve is the entire point of reading bodies."""
    body = ("Die Kommission prüft neue Vorgaben für den Versandhandel. " * 8 +
            "Betroffen ist unter anderem Temu.")
    reasons, fields = select("Neue Vorgaben", "https://x.test/news/neue-vorgaben", body)
    assert reasons == ("brand:Temu",)
    assert "body" in fields


def test_a_narrow_topic_in_a_body_also_counts_alone():
    """Plain 'body needs two' would drop these; measured, it dropped a real GPSR find."""
    body = "Der Verband informiert seine Mitglieder. " * 8 + "Stichwort GPSR."
    reasons, fields = select("Verbraucherrecht", "https://x.test/news/verbraucherrecht", body)
    assert "topic:GPSR" in reasons
    assert "body" in fields


def test_the_title_still_matches_on_a_single_keyword():
    """The asymmetry is the design: OR on the title, co-occurrence in the body."""
    reasons, fields = select("Debatte über die Lieferkette", "https://x.test/news/debatte")
    assert reasons == ("keyword:supply chain",)
    assert fields == ("title",)
