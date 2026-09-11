"""The customer's keyword list of 2026-09-10 (docs/极兔关键词.md), row by row.

Rows 1 and 2 became brands, row 3 became a parcel-sector keyword plus body-only policy
keywords, rows 4 and 5 became clients/jt-express/alert_taxonomy.json and are
deliberately absent from matching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.selector import select_candidate
from src.profile import load_profile

PROFILE = load_profile("jt-express")
PROFILE_PATH = Path("clients/jt-express/profile.json")


def select(title, url, body=None):
    return select_candidate(title, url, PROFILE, body)


def test_competitor_brands_stand_alone_everywhere():
    """Rows 1 and 2: a competitor is a brand, so one hit anywhere selects, and
    the role travels with the profile version."""
    reasons, _ = select("DHL erhöht die Paketpreise", "https://x.test/news/dhl-preise")
    assert "brand:DHL" in reasons
    body = "Lange Vorrede über den Onlinehandel. " * 10 + "Auch GLS reagiert."
    reasons, fields = select("Ohne Marke", "https://x.test/news/ohne-marke", body)
    assert "brand:GLS" in reasons and "body" in fields
    roles = {b["name"]: b.get("role")
             for b in json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["brands"]}
    assert roles["DHL"] == "competitor"
    assert roles["J&T"] == "own"
    assert roles["Temu"] == "customer"


def test_ups_is_the_carrier_not_a_suffix():
    """Case-insensitive 'ups' found 13 items in the corpus, all start-ups."""
    assert "brand:UPS" not in select(
        "Zahl der Logistik-Start-ups wächst",
        "https://x.test/blog/zahl-der-logistik-start-ups-waechst")[0]
    assert "brand:UPS" in select("UPS-Strategie für Europa",
                                 "https://x.test/news/ups-strategie-fuer-europa")[0]
    assert "brand:UPS" in select(None, "https://x.test/news/streik-bei-ups")[0]


def test_ups_compounds_found_in_stored_bodies():
    """The four non-carrier '-ups' left in the corpus after start-ups; Set-ups
    made a warehouse opening a UPS brand find. Slugs turn the hyphen into a space."""
    for compound in ("Scale-ups", "Grown-ups", "Back-ups", "Set-ups"):
        body = f"Die Kapazitäten des bisherigen {compound} reichten nicht. " * 4
        assert "brand:UPS" not in select("Ohne Marke", "https://x.test/news/x", body)[0]
        slug = f"https://x.test/news/{compound.lower()}-im-fokus"
        assert "brand:UPS" not in select(None, slug)[0]
    body = "Retoure unverpackt in einem DHL- oder UPS-Paketshop abgeben. " * 4
    assert "brand:UPS" in select("Ohne Marke", "https://x.test/news/x", body)[0]


def test_policy_words_are_body_only():
    """Row 3 is 'Logistik AND Gesetz'. The policy side never selects a title on
    its own - measured, 58 headlines and none about logistics - but it is the
    second keyword the body rule asks for."""
    title = "Neue Verordnung beschlossen"
    url = "https://x.test/news/neue-verordnung-beschlossen"
    assert select(title, url)[0] == ()

    body = ("Die Verordnung gilt ab Januar. " * 6 +
            "Für Paketdienste bedeutet das neue Meldepflichten.")
    reasons, fields = select(title, url, body)
    assert "keyword:regulatory instrument" in reasons
    assert "keyword:parcel sector" in reasons
    assert "body" in fields

    policy_only = "Die Verordnung gilt ab Januar. Die Richtlinie folgt später. " * 6
    assert select(title, url, policy_only)[0] == ()


def test_two_body_only_words_do_not_make_a_pair():
    """Verordnung plus Bußgeld with no sector word is a law page, not market
    news: that pattern selected Wettbewerbszentrale category pages."""
    body = ("Die Verordnung sieht ein Bußgeld vor, das die Behörde nach einem "
            "Urteil verhängt. ") * 6
    assert select("Recht", "https://x.test/recht/verordnung-bussgeld", body)[0] == ()
    with_anchor = body + " Betroffen sind vor allem Paketdienste."
    reasons, _ = select("Recht", "https://x.test/recht/verordnung-bussgeld", with_anchor)
    assert "keyword:parcel sector" in reasons and "keyword:enforcement" in reasons


def test_sector_word_alone_selects_a_title_but_not_a_body():
    """The cheap LLM reads titles, so one sector word there is enough; nothing
    cheap reads bodies, so there it needs a partner."""
    assert "keyword:parcel sector" in select(
        "Zusteller streiken", "https://x.test/news/zusteller-streiken")[0]
    body = "Der Zusteller kam wie immer pünktlich. " * 8
    assert select("Ohne Bezug", "https://x.test/news/ohne-bezug", body)[0] == ()


def test_incident_vocabulary_is_not_a_matching_rule():
    """Rows 4 and 5 classify, they do not select: bare 'Streik' or 'Unfall' hit
    333 titles in the corpus and not one co-occurred with a brand."""
    assert select("Tödlicher Absturz in den Dolomiten",
                  "https://x.test/panorama/toedlicher-absturz-dolomiten")[0] == ()
    assert select("Streik legt Flughafen lahm",
                  "https://x.test/wirtschaft/streik-flughafen")[0] == ()
    taxonomy = json.loads(
        Path("clients/jt-express/alert_taxonomy.json").read_text(encoding="utf-8"))
    assert {t["id"] for t in taxonomy["alert_types"]} >= {"strike", "accident", "data_breach"}


def test_profile_rejects_unknown_scope(tmp_path):
    bad = tmp_path / "profile.json"
    bad.write_text(json.dumps({"slug": "x", "profile_version": "1", "keywords": [
        {"name": "k", "terms": ["k"], "scope": "title"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="scope"):
        load_profile(bad)
