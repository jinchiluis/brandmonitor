"""Tests for the body gate. The model call is a fake; nothing touches the network."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.body_gate import (GateConfigError, GateItem, parse_reply, pending_items,  # noqa: E402
                           prompt_version, render_item, render_system_prompt, run_body_gate)
from src.db import migrate  # noqa: E402
from src.profile import load_profile  # noqa: E402
from src.selector import Candidate  # noqa: E402

PROFILE = load_profile("jt-express")

TEMU_BODY = "Temu senkt erneut die Preise für Kleidung und Elektronik. " * 5
DSA_BODY = ("Die Bundesnetzagentur setzt den Digital Services Act gegenüber einem "
            "Online-Marktplatz durch und verlangt Angaben zu Händlern. ") * 3
PLAIN_BODY = "Das Wetter bleibt am Wochenende sonnig und warm. " * 5


def reply(verdict, reason="because"):
    return json.dumps({"reason": reason, "verdict": verdict})


class FakeModel:
    """Replies from a script, one entry per call; an exception entry is raised."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, {"in": 1000, "out": 50}


@pytest.fixture
def corpus(tmp_path):
    """A migrated database with two configured sources, and a row writer."""
    db = tmp_path / "gate.sqlite3"
    migrate(db)
    news = tmp_path / "news.json"
    news.write_text(json.dumps([{"url": "https://news.test/", "content_mode": "full_text"}]),
                    encoding="utf-8")
    regulatory = tmp_path / "regulatory.json"
    regulatory.write_text(json.dumps([{"url": "https://reg.test/", "content_mode": "full_text"}]),
                          encoding="utf-8")

    def add(kind, ext, title, body=None, version=1, published="2026-09-10T08:00:00+00:00"):
        slug = "news.test" if kind == "news" else "reg.test"
        payload = {"discovered_via": "feed"}
        if body:
            payload["body_text"] = body
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO raw_item (source_slug, source_kind, external_id, version, url, title, "
            "published_at, fetched_at, content_hash, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, kind, ext, version, f"https://{slug}/a/{ext}", title, published,
             published, f"{ext}-{version}", json.dumps(payload)))
        conn.commit()
        conn.close()

    return {"db": db, "news": news, "regulatory": regulatory, "add": add}


def stored(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT a.*, r.external_id FROM assessment a JOIN raw_item r ON r.id = a.raw_item_id "
        "ORDER BY r.external_id")]
    conn.close()
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


def item(n, body=TEMU_BODY, reasons=("brand:Temu",)):
    candidate = Candidate(source_slug="news.test", title=f"Temu {n}",
                          url=f"https://news.test/a/{n}", published_at=None, reasons=reasons,
                          matched_in=("body",), external_id=str(n), has_body=True)
    return GateItem(candidate, raw_item_id=n, body=body)


# ── prompts: shared wording, the client's inputs from its profile ────────

def test_news_prompt_is_built_from_the_profile():
    system = render_system_prompt(PROFILE, "news")
    assert system.startswith("You screen news articles for J&T Express Germany")
    assert "Competitors: DHL, Hermes, UPS, FedEx, DPD, GLS, GoFo, iMile." in system
    # Measured: "a competitor as a business" kept DHL Freight appointments.
    assert "not its freight\n  forwarding, contract logistics or supply-chain divisions" in system
    for line in (PROFILE.prompt.relevant + PROFILE.prompt.false_matches
                 + PROFILE.prompt.body_false_matches):
        assert f"- {line}" in system
    assert "{" not in system


def test_regulatory_prompt_lists_legal_areas_not_business_topics():
    system = render_system_prompt(PROFILE, "regulatory")
    assert system.startswith("You screen official publications for J&T Express Germany")
    for line in PROFILE.prompt.regulatory_relevant + PROFILE.prompt.regulatory_false_matches:
        assert f"- {line}" in system
    assert f"- {PROFILE.prompt.relevant[0]}" not in system
    assert "relevant if any one item is" in system
    assert "{" not in system


def test_a_profile_without_legal_areas_can_gate_news_but_not_regulatory(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "slug": "acme", "profile_version": "1", "brands": [{"name": "Acme", "terms": ["acme"]}],
        "prompt": {"about": "a shop", "relevant": ["shops"]}}), encoding="utf-8")
    profile = load_profile(path)
    assert "- none listed" in render_system_prompt(profile, "news")
    with pytest.raises(GateConfigError, match="regulatory_relevant"):
        render_system_prompt(profile, "regulatory")


def test_an_empty_false_match_list_leaves_no_blank_bullet(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "slug": "acme", "profile_version": "1", "brands": [{"name": "Acme", "terms": ["acme"]}],
        "prompt": {"about": "a shop", "relevant": ["shops"],
                   "regulatory_relevant": ["consumer law for shops"]}}), encoding="utf-8")
    system = render_system_prompt(load_profile(path), "regulatory")
    assert "Not relevant:\n- speeches" in system


def test_an_unknown_template_placeholder_is_a_config_error():
    with pytest.raises(GateConfigError):
        render_system_prompt(PROFILE, "news", template="For {name}: {mood}")


def test_prompt_version_names_the_stage_and_tier_and_follows_the_text():
    news = render_system_prompt(PROFILE, "news")
    assert prompt_version("news", news).startswith("body_gate-news-")
    assert prompt_version("news", news) != prompt_version("news", news + " ")
    assert prompt_version("regulatory", news).startswith("body_gate-regulatory-")


def test_news_items_show_their_matched_rules_and_regulatory_items_do_not():
    gate_item = item(1, body="x" * 50, reasons=("brand:Temu", "keyword:customs"))
    assert render_item(gate_item, "news", body_chars=10) == (
        "[Temu, customs] news.test\nTITLE: Temu 1\n\n" + "x" * 10)
    assert render_item(gate_item, "regulatory", body_chars=10) == (
        "news.test\nTITLE: Temu 1\n\n" + "x" * 10)


@pytest.mark.parametrize("text, expected", [
    (reply("relevant", " Temu fees "), ("relevant", "Temu fees")),
    (reply("unsure"), ("unsure", "because")),
    ('{"verdict": "keep", "reason": "x"}', None),
    ('{"verdict": "relevant"}', None),
    ("relevant: Temu fees", None),
    ("", None),
    ("[]", None),
])
def test_reply_parsing(text, expected):
    assert parse_reply(text) == expected


# ── input: selected bodies without a decision, each item once ────────────

def test_only_selected_bodies_without_a_decision_are_offered_newest_first(corpus):
    add = corpus["add"]
    add("news", "old", "Temu alt", TEMU_BODY, published="2026-09-01T08:00:00+00:00")
    add("news", "new", "Temu neu", TEMU_BODY, published="2026-09-10T08:00:00+00:00")
    add("news", "title-only", "Temu Titel")
    add("news", "weather", "Wetter", PLAIN_BODY)
    add("regulatory", "dsa", "DSC gegen Marktplatz", DSA_BODY)

    news, already = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    assert [i.candidate.external_id for i in news] == ["new", "old"]
    assert already == 0
    assert news[0].body == TEMU_BODY
    regulatory, _ = pending_items(PROFILE, "regulatory", corpus["db"], corpus["regulatory"])
    assert [i.candidate.external_id for i in regulatory] == ["dsa"]


def test_gated_items_are_not_offered_again_and_a_restamp_is_not_new(corpus):
    corpus["add"]("news", "n1", "Temu", TEMU_BODY)
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    run_body_gate(items, PROFILE, "news", FakeModel(reply("relevant")),
                  db_path=corpus["db"], workers=1)

    assert pending_items(PROFILE, "news", corpus["db"], corpus["news"]) == ([], 1)
    corpus["add"]("news", "n1", "Temu", TEMU_BODY + " Nachtrag.", version=2)
    assert pending_items(PROFILE, "news", corpus["db"], corpus["news"]) == ([], 1)


def test_regate_offers_items_decided_under_another_prompt_version(corpus):
    corpus["add"]("news", "n1", "Temu", TEMU_BODY)
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    run_body_gate(items, PROFILE, "news", FakeModel(reply("relevant")),
                  db_path=corpus["db"], workers=1)
    current = prompt_version("news", render_system_prompt(PROFILE, "news"))

    again, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"],
                             regate_from=current)
    assert again == []
    changed, already = pending_items(PROFILE, "news", corpus["db"], corpus["news"],
                                     regate_from="body_gate-news-000000000000")
    assert [i.candidate.external_id for i in changed] == ["n1"] and already == 0


# ── the gate: every decision stored, failures fall towards keeping ───────

def test_verdicts_are_stored_as_assessments_with_their_versions(corpus):
    for ext in ("a", "b", "c"):
        corpus["add"]("news", ext, f"Temu {ext}", TEMU_BODY,
                      published=f"2026-09-0{'abc'.index(ext) + 1}T08:00:00+00:00")
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])  # c, b, a
    model = FakeModel(reply("relevant", "Temu prices"), reply("unsure"), reply("irrelevant"))

    result = run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1,
                           model="test-model")

    assert (result.count("relevant"), result.count("unsure"), result.count("irrelevant")) == (1, 1, 1)
    assert (result.input_tokens, result.output_tokens) == (3000, 150)
    rows = {row["external_id"]: row for row in stored(corpus["db"])}
    assert {k: r["relevant"] for k, r in rows.items()} == {"c": 1, "b": None, "a": 0}
    row = rows["c"]
    assert row["client_slug"] == "jt-express"
    assert row["profile_version"] == PROFILE.profile_version
    assert row["prompt_version"] == result.prompt_version
    assert row["prompt_version"].startswith("body_gate-news-")
    assert row["payload"]["verdict"] == "relevant"
    assert row["payload"]["reason"] == "Temu prices"
    assert row["payload"]["model"] == "test-model"
    assert row["payload"]["selector_reasons"] == ["brand:Temu"]
    assert row["payload"]["fail_open"] is False
    # The model saw the matched rule, the title and the body.
    assert model.calls[0][1].startswith("[Temu] news.test\nTITLE: Temu c\n\nTemu senkt")


def test_an_unusable_reply_is_retried_once(corpus):
    corpus["add"]("news", "n1", "Temu", TEMU_BODY)
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    model = FakeModel("I think it is relevant.", reply("irrelevant"))

    result = run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1)

    assert len(model.calls) == 2
    assert result.count("irrelevant") == 1 and result.fail_open == 0


def test_an_item_that_fails_twice_is_kept_as_unsure(corpus):
    corpus["add"]("news", "n1", "Temu", TEMU_BODY)
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    model = FakeModel(TimeoutError("read timed out"), "")

    result = run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1)

    assert result.fail_open == 1 and result.stopped is None
    [row] = stored(corpus["db"])
    assert row["relevant"] is None
    assert row["payload"]["fail_open"] is True
    assert row["payload"]["error"].startswith("unusable reply")


def test_three_failures_in_a_row_stop_the_run_and_leave_the_rest_ungated(corpus):
    items = [item(n) for n in range(1, 6)]
    for n in range(1, 6):
        corpus["add"]("news", str(n), f"Temu {n}", TEMU_BODY)
    model = FakeModel(*[TimeoutError("down")] * 6)

    result = run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1)

    assert result.stopped and "3 items in a row failed" in result.stopped
    assert len(result.decisions) == 3 and len(model.calls) == 6
    assert len(stored(corpus["db"])) == 3


def test_a_success_resets_the_failure_count(corpus):
    items = [item(n) for n in range(1, 5)]
    for n in range(1, 5):
        corpus["add"]("news", str(n), f"Temu {n}", TEMU_BODY)
    # Two items fail, one succeeds, the fourth succeeds on its retry: never three in a row.
    model = FakeModel(TimeoutError("x"), TimeoutError("x"), TimeoutError("x"), TimeoutError("x"),
                      reply("relevant"), TimeoutError("x"), reply("irrelevant"))

    result = run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1)

    assert result.stopped is None and len(result.decisions) == 4
    assert result.fail_open == 2 and model.replies == []


def test_a_config_error_aborts_and_stores_nothing(corpus):
    corpus["add"]("news", "n1", "Temu", TEMU_BODY)
    items, _ = pending_items(PROFILE, "news", corpus["db"], corpus["news"])
    model = FakeModel(GateConfigError("AuthenticationError: bad key"))

    with pytest.raises(GateConfigError):
        run_body_gate(items, PROFILE, "news", model, db_path=corpus["db"], workers=1)
    assert stored(corpus["db"]) == []


def test_no_items_means_no_call(corpus):
    model = FakeModel()
    result = run_body_gate([], PROFILE, "news", model, db_path=corpus["db"])
    assert model.calls == [] and result.decisions == []
