"""The manual funnel backfill is frozen, resumable and client-specific."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import migrate  # noqa: E402
from tools.backfill_funnel import (CampaignError, campaign_status, create_campaign,  # noqa: E402
                                   decide_titles, gate_existing_bodies,
                                   promote_title_keeps, shadow_gate_title_keeps)
from src.body_gate import pending_items  # noqa: E402
from src.profile import load_profile  # noqa: E402


BODY = "Acme betreibt einen Paketdienst für Online-Händler in Deutschland. " * 5
REGULATORY_BODY = (
    "Die Behörde erlässt neue Vorschriften für Acme und Online-Marktplätze. " * 5)


class FakeCaller:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.answers.pop(0), {"in": 100, "out": 5}


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "corpus.sqlite3"
    migrate(db)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "slug": "acme", "name": "Acme", "profile_version": "2026-09-11.1",
        "brands": [{"name": "Acme", "role": "own", "terms": ["Acme"]}],
        "topics": [{"name": "parcel", "terms": ["Paketdienst"]}],
        "prompt": {
            "about": "a parcel carrier",
            "relevant": ["Material developments involving Acme or parcel delivery."],
            "regulatory_relevant": ["Rules affecting parcel carriers or marketplaces."],
        },
    }), encoding="utf-8")
    news = tmp_path / "news.json"
    news.write_text(json.dumps([
        {"url": "https://title.test/", "content_mode": "title_only"},
        {"url": "https://full.test/", "content_mode": "full_text"},
    ]), encoding="utf-8")
    regulatory = tmp_path / "regulatory.json"
    regulatory.write_text(json.dumps([
        {"url": "https://reg.test/", "content_mode": "full_text"},
    ]), encoding="utf-8")

    def add(slug, kind, external_id, title, body=None, version=1):
        payload = {"url": external_id, "title": title, "discovered_via": "feed"}
        if body:
            payload["body_text"] = body
        conn = sqlite3.connect(db)
        cursor = conn.execute(
            "INSERT INTO raw_item (source_slug, source_kind, external_id, version, url, "
            "title, published_at, fetched_at, content_hash, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, kind, external_id, version, external_id, title,
             "2026-09-10", "2026-09-10T08:00:00+00:00",
             f"{external_id}-{version}", json.dumps(payload)))
        raw_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return raw_id

    add("title.test", "news", "https://title.test/one", "Acme expands")
    add("title.test", "news", "https://title.test/two", "Acme opens depot")
    add("title.test", "news", "https://title.test/body", "Acme parcel hub", BODY)
    add("full.test", "news", "https://full.test/acme", "Acme report", BODY)
    add("full.test", "news", "https://full.test/weather", "Weather", "Sunny weather " * 30)
    add("reg.test", "regulatory", "https://reg.test/rule", "Acme regulation",
        REGULATORY_BODY)

    root = tmp_path / "campaigns"
    return {"db": db, "profile": profile, "news": news, "regulatory": regulatory,
            "root": root, "add": add}


def make_campaign(project, name="initial"):
    return create_campaign(
        name, client=project["profile"], db_path=project["db"],
        news_sources=project["news"], regulatory_sources=project["regulatory"],
        root=project["root"])


def test_create_freezes_all_three_historical_lanes(project):
    metadata = make_campaign(project)

    assert metadata["counts"] == {
        "body_news_pending": 2,
        "body_news_already_gated": 0,
        "body_regulatory_pending": 1,
        "body_regulatory_already_gated": 0,
        "title_pending": 2,
        "title_with_body": 1,
    }
    directory = project["root"] / "initial"
    assert len((directory / "body-news.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert len((directory / "body-regulatory.jsonl").read_text(
        encoding="utf-8").splitlines()) == 1
    assert len((directory / "title-pending.jsonl").read_text(
        encoding="utf-8").splitlines()) == 2


def test_title_decisions_are_isolated_bounded_and_resumable(project):
    make_campaign(project)
    result = decide_titles(
        "initial", limit=1, root=project["root"], caller=FakeCaller("1"))
    assert result["attempted"] == 1 and result["remaining"] == 1

    result = decide_titles(
        "initial", limit=1, root=project["root"], caller=FakeCaller("0"))
    assert result["attempted"] == 1 and result["remaining"] == 0
    result = decide_titles(
        "initial", limit=1, root=project["root"], caller=FakeCaller())
    assert result["attempted"] == 0

    decision_dir = project["root"] / "initial" / "title-decisions" / "acme"
    assert len(list(decision_dir.glob("*.jsonl"))) == 1
    assert not (project["root"] / "title_gate").exists()
    assert campaign_status("initial", root=project["root"])["titles"] == {
        "decided": 2, "keep": 1, "drop": 1, "fail_open": 0}


def test_body_verdict_is_customer_specific_and_raw_item_stays_shared(project):
    make_campaign(project)
    model = FakeCaller(json.dumps({"verdict": "relevant", "reason": "Acme expansion"}))

    result = gate_existing_bodies(
        "initial", "news", limit=1, root=project["root"], caller=model, workers=1)

    assert result["relevant"] == 1 and result["remaining"] == 1
    conn = sqlite3.connect(project["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM assessment").fetchone()
    raw = conn.execute("SELECT payload FROM raw_item WHERE id=?", (row["raw_item_id"],)).fetchone()
    conn.close()
    assert row["client_slug"] == "acme"
    assert row["prompt_version"].startswith("body_gate-news-")
    assert "relevant" not in json.loads(raw["payload"])


def test_only_the_requested_number_of_keeps_enters_the_body_queue(project):
    make_campaign(project)
    decide_titles(
        "initial", limit=2, root=project["root"], caller=FakeCaller("1 2"))

    result = promote_title_keeps("initial", limit=1, root=project["root"])

    assert result == {
        "promoted": 1, "already_promoted": 0, "keeps": 2, "remaining": 1}
    conn = sqlite3.connect(project["db"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM body_fetch").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    route = json.loads(rows[0]["hint_payload"])["title_gate_routes"]["acme"]
    assert route["backfill_campaign"] == "initial"


def test_a_fetched_keep_skips_the_body_gate_and_is_ready_for_assessor(project):
    make_campaign(project)
    decide_titles(
        "initial", limit=2, root=project["root"], caller=FakeCaller("1"))
    promote_title_keeps("initial", limit=1, root=project["root"])
    conn = sqlite3.connect(project["db"])
    external_id = conn.execute("SELECT external_id FROM body_fetch").fetchone()[0]
    conn.close()
    project["add"]("title.test", "news", external_id, "Acme opens depot", BODY, version=2)
    conn = sqlite3.connect(project["db"])
    conn.execute("UPDATE body_fetch SET status='ok' WHERE external_id=?", (external_id,))
    conn.commit()
    conn.close()

    items, _already = pending_items(
        load_profile(project["profile"]), "news", project["db"], project["news"])
    assert external_id not in {item.candidate.external_id for item in items}
    status = campaign_status("initial", root=project["root"])
    assert status["title_keeps"] == {
        "keeps": 1, "promoted": 1, "fetched": 1, "ready_for_assessor": 1}


def test_shadow_body_gate_is_resumable_and_does_not_change_title_routing(project):
    make_campaign(project)
    decide_titles("initial", limit=2, root=project["root"], caller=FakeCaller("1"))
    promote_title_keeps("initial", limit=1, root=project["root"])
    conn = sqlite3.connect(project["db"])
    external_id = conn.execute("SELECT external_id FROM body_fetch").fetchone()[0]
    conn.close()
    raw_id = project["add"](
        "title.test", "news", external_id, "Acme opens depot", BODY, version=2)
    conn = sqlite3.connect(project["db"])
    conn.execute("UPDATE body_fetch SET status='ok' WHERE external_id=?", (external_id,))
    conn.commit()
    conn.close()

    answer = json.dumps({"verdict": "irrelevant", "reason": "Only a test"})
    result = shadow_gate_title_keeps(
        "initial", root=project["root"], caller=FakeCaller(answer), workers=1)

    assert result["attempted"] == 1
    assert result["cumulative"] == {"relevant": 0, "unsure": 0, "irrelevant": 1}
    assert result["routing_changed"] is False
    rows = (project["root"] / "initial" / "title-body-gate-shadow.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert json.loads(rows[0])["raw_item_id"] == raw_id
    conn = sqlite3.connect(project["db"])
    assert conn.execute("SELECT COUNT(*) FROM assessment").fetchone()[0] == 0
    conn.close()

    rerun = shadow_gate_title_keeps(
        "initial", root=project["root"], caller=FakeCaller(), workers=1)
    assert rerun["attempted"] == 0 and rerun["remaining"] == 0


def test_changed_profile_refuses_to_continue_a_frozen_campaign(project):
    make_campaign(project)
    profile = json.loads(project["profile"].read_text(encoding="utf-8"))
    profile["profile_version"] = "2026-09-11.2"
    project["profile"].write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(CampaignError, match="changed after this campaign"):
        campaign_status("initial", root=project["root"])


def test_keeps_cannot_be_promoted_before_title_review_is_complete(project):
    make_campaign(project)
    decide_titles("initial", limit=1, root=project["root"], caller=FakeCaller("1"))

    with pytest.raises(CampaignError, match="title review is incomplete"):
        promote_title_keeps("initial", limit=1, root=project["root"])
