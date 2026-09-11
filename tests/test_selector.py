"""Tests for the deterministic, read-only candidate selector (news and regulatory)."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.selector import (format_report, pdf_filename_text, select_candidate,
                          slug_text, selection_from_db, url_metadata)  # noqa: E402
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


@pytest.mark.parametrize("url, label", [
    ("https://www.spiegel.de/ausland/uno-fortschritt-bei-regulierung-von-killer-robotern"
     "-a-83fa6d5d-2259-4ee2-b683-284b2e6de254",
     "uno fortschritt bei regulierung von killer robotern"),
    ("https://www.sueddeutsche.de/wirtschaft/sz-podcast-temu-und-shein-li.3544550",
     "sz podcast temu und shein"),
    ("https://www.sueddeutsche.de/projekte/artikel/politik/china-energiekrise-e171345/",
     "china energiekrise"),
    ("https://www.faz.net/aktuell/dhl-pakete-mit-brandsaetzen-accg-201146486.html",
     "dhl pakete mit brandsaetzen"),
    ("https://www.faz.net/aktuell/china-haft-fuer-kuenstler-201160751.html",
     "china haft fuer kuenstler"),
    ("https://www.zeit.de/wirtschaft/2026-09/shein-boersengang-hongkong-gxe",
     "shein boersengang hongkong"),
    ("https://www.bvl.de/blog/novelle-des-postgesetzes%ef%bf%bc/", "novelle des postgesetzes"),
])
def test_publisher_id_tails_are_stripped_from_slugs(url, label):
    """The slug is what the title gate's LLM reads when a source ships no title."""
    assert slug_text(url) == label


@pytest.mark.parametrize("url, label", [
    ("https://x.test/news/maxibrief-2027", "maxibrief 2027"),
    ("https://x.test/news/dax-steigt-auf-24000", "dax steigt auf 24000"),
])
def test_years_and_short_numbers_survive_tail_stripping(url, label):
    assert slug_text(url) == label


def test_tail_stripping_changes_no_match():
    """No rule can match inside an ID, so stripping one never loses a selection."""
    url = ("https://www.spiegel.de/wirtschaft/temu-shein-aliexpress-billigpaketimport"
           "-a-af3e1115-0244-4b14-934c-dd1e95367275")
    reasons, fields = select(None, url)
    assert {"brand:Temu", "brand:Shein", "brand:AliExpress"} <= set(reasons)
    assert fields == ("slug",)


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


def test_db_selection_respects_excluded_dirs(tmp_path):
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)"
    )
    conn.execute("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", (
        "news.test", "news", "n1", 1, "https://news.test/paid/temu-story",
        "Temu story", None, "2026-09-02", '{"discovered_via":"rss"}'))
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([{
        "url": "https://news.test/", "content_mode": "full_text",
        "excluded_dirs": ["paid"],
    }]), encoding="utf-8")

    result = selection_from_db(db, ((sources, "news"),), "all", PROFILE)

    assert result.eligible == 0
    assert result.candidates == ()


def test_db_selection_respects_source_specific_url_fragments(tmp_path):
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)"
    )
    conn.execute("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", (
        "news.test", "news", "n1", 1,
        "https://news.test/news/temu-newsuebersicht-123.html",
        "Temu Newsübersicht", None, "2026-09-02", '{"discovered_via":"rss"}'))
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([{
        "url": "https://news.test/", "content_mode": "full_text",
        "excluded_url_substrings": ["newsuebersicht"],
    }]), encoding="utf-8")

    result = selection_from_db(db, ((sources, "news"),), "all", PROFILE)

    assert result.eligible == 0
    assert result.candidates == ()


def test_db_selection_respects_source_specific_title_fragments(tmp_path):
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)"
    )
    conn.execute("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", (
        "news.test", "news", "n1", 1,
        "https://news.test/news/personalie-123.html",
        "Temu Newsübersicht", None, "2026-09-02", '{"discovered_via":"rss"}'))
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([{
        "url": "https://news.test/", "content_mode": "full_text",
        "excluded_title_substrings": ["Newsübersicht"],
    }]), encoding="utf-8")

    result = selection_from_db(db, ((sources, "news"),), "all", PROFILE)

    assert result.eligible == 0
    assert result.candidates == ()


def test_first_seen_in_selects_only_articles_new_in_that_run(tmp_path):
    """The daily gate sees each article once. A restamp writes version 2 of an old
    URL; that is not a new article and must not reach the LLM again."""
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, first_run_id INTEGER, payload TEXT)")
    feed = '{"discovered_via":"feed"}'
    conn.executemany("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("news.test", "news", "old", 1, "https://news.test/a/temu-alt", "Temu alt",
         "2026-09-01", "2026-09-01", 1, feed),
        ("news.test", "news", "old", 2, "https://news.test/a/temu-alt", "Temu alt",
         "2026-09-02", "2026-09-02", 2, feed),
        ("news.test", "news", "new", 1, "https://news.test/a/temu-neu", "Temu neu",
         "2026-09-02", "2026-09-02", 2, feed),
    ])
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([{"url": "https://news.test/"}]), encoding="utf-8")
    specs = ((sources, "news"),)

    today = selection_from_db(db, specs, "title-only", PROFILE, first_seen_in={2})
    assert [c.external_id for c in today.candidates] == ["new"]
    assert today.eligible == 1
    first = selection_from_db(db, specs, "title-only", PROFILE, first_seen_in={1})
    assert [c.external_id for c in first.candidates] == ["old"]
    everything = selection_from_db(db, specs, "title-only", PROFILE)
    assert {c.external_id for c in everything.candidates} == {"old", "new"}


def test_candidates_say_whether_a_body_is_stored(tmp_path):
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)")
    conn.executemany("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", [
        ("news.test", "news", "t", 1, "https://news.test/a/temu-titel", "Temu Titel",
         None, "2026-09-02", '{"discovered_via":"feed"}'),
        ("news.test", "news", "b", 1, "https://news.test/a/temu-body", "Temu Body",
         None, "2026-09-02", json.dumps({"discovered_via": "feed",
                                         "body_text": "Temu und der Zoll."})),
    ])
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([{"url": "https://news.test/"}]), encoding="utf-8")

    result = selection_from_db(db, ((sources, "news"),), "title-only", PROFILE)

    assert {c.external_id: c.has_body for c in result.candidates} == {"t": False, "b": True}
    assert {c.source_kind for c in result.candidates} == {"news"}


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


def test_bodies_without_a_page_date_are_skipped_only_where_articles_carry_one(tmp_path):
    """A section index on a trade-press site has a body full of teasers and no
    page date, because it is not an article. The same absence on a regulator or
    a small source proves nothing, so the rule never applies there."""
    db = tmp_path / "items.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE raw_item (source_slug TEXT, source_kind TEXT, "
        "external_id TEXT, version INTEGER, url TEXT, title TEXT, "
        "published_at TEXT, fetched_at TEXT, payload TEXT)")

    def body(source, n, dated):
        payload = json.dumps({"discovered_via": "sitemap",
                              "published_at_source": "page" if dated else "sitemap",
                              "body_text": "Temu und der Zoll. " * 3})
        return (source, "news", f"{source}-{n}", 1, f"https://{source}/temu-story-{n}",
                "Temu story", "2026-09-02", "2026-09-02", payload)

    rows = [body("dated.test", i, True) for i in range(20)] + [body("dated.test", 99, False)]
    rows += [body("small.test", i, True) for i in range(4)] + [body("small.test", 99, False)]
    conn.executemany("INSERT INTO raw_item VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    sources = tmp_path / "news.json"
    sources.write_text(json.dumps([
        {"url": "https://dated.test/", "content_mode": "full_text"},
        {"url": "https://small.test/", "content_mode": "full_text"}]), encoding="utf-8")

    result = selection_from_db(db, ((sources, "news"),), "all", PROFILE)

    assert (result.hub_suspects, result.eligible) == (1, 25)
    urls = {item.url for item in result.candidates}
    assert "https://dated.test/temu-story-99" not in urls
    assert "https://small.test/temu-story-99" in urls
    assert "Skipped 1 stored bodies" in format_report(
        result.candidates, result.eligible, result.unknowns, result.hub_suspects)


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
