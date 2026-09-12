"""Tests for the title gate. The model call is a fake; nothing touches the network."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.selector import Candidate  # noqa: E402
from src.profile import load_profile  # noqa: E402
from src.title_gate import (GateConfigError, parse_reply, prompt_version,  # noqa: E402
                            logged_keeps, prune_logs, render_batch,
                            render_system_prompt, run_gate)

PROFILE = load_profile("jt-express")


def candidate(n, title="Temu senkt Preise", reasons=("brand:Temu",), source="spiegel.de"):
    return Candidate(source_slug=source, title=title, url=f"https://{source}/a/item-{n}",
                     published_at=None, reasons=reasons, matched_in=("title",),
                     external_id=f"https://{source}/a/item-{n}")


class FakeModel:
    """Replies from a script, one entry per call; an exception entry is raised."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, {"in": 100, "out": 5}


# ── reply parsing is strict, because a drop is final ─────────────────────

@pytest.mark.parametrize("text, n, expected", [
    ("2 7", 10, [2, 7]),
    ("  1, 3\n", 10, [1, 3]),
    ("3 3", 10, [3]),
    ("0", 10, []),
    ("10", 10, [10]),
])
def test_usable_replies(text, n, expected):
    assert parse_reply(text, n) == expected


@pytest.mark.parametrize("text, n", [
    ("", 10),            # silence is not "none"
    ("Keep 2 and 7", 10),
    ("7 8 12", 7),       # measured: Opus with thinking off named an item that did not exist
    ("0 3", 10),
    ("11", 10),
])
def test_unusable_replies(text, n):
    assert parse_reply(text, n) is None


# ── the prompt is universal; the client comes from its profile ───────────

def test_prompt_is_built_from_the_profile():
    system = render_system_prompt(PROFILE)
    assert system.startswith("You screen news items for J&T Express Germany, a parcel carrier")
    assert "J&T (极兔) is the client." in system
    assert "Competitors: DHL, Hermes, UPS, FedEx, DPD, GLS, GoFo, iMile." in system
    assert "Customers: Temu, Shein, AliExpress, TikTok Shop." in system
    for line in PROFILE.prompt.relevant + PROFILE.prompt.false_matches:
        assert f"- {line}" in system
    assert "{" not in system
    assert system.endswith('Reply "0" if none. No other text.')


def test_prompt_version_follows_the_text():
    system = render_system_prompt(PROFILE)
    assert prompt_version(system) == prompt_version(system)
    assert prompt_version(system) != prompt_version(system + " ")


def test_a_profile_without_a_prompt_section_cannot_be_gated(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"slug": "acme", "profile_version": "1",
                                "brands": [{"name": "Acme", "terms": ["acme"]}]}),
                    encoding="utf-8")
    with pytest.raises(GateConfigError):
        render_system_prompt(load_profile(path))


def test_an_unknown_template_placeholder_is_a_config_error():
    with pytest.raises(GateConfigError):
        render_system_prompt(PROFILE, template="For {name}: {mood}")


def test_profile_rejects_a_malformed_prompt_section(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"slug": "acme", "profile_version": "1",
                                "brands": [{"name": "Acme", "terms": ["acme"]}],
                                "prompt": {"about": "a shop", "relevant": []}}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="prompt.relevant"):
        load_profile(path)


def test_batch_lines_show_matched_rules_source_and_label():
    item = Candidate(source_slug="zeit.de", title=None,
                     url="https://www.zeit.de/wirtschaft/shein-boersengang-hongkong-gxe",
                     published_at=None, reasons=("brand:Shein", "keyword:China/chinesisch"),
                     matched_in=("slug",))
    assert render_batch([item]) == (
        "1. [Shein, China/chinesisch] zeit.de: shein boersengang hongkong")


# ── the gate ─────────────────────────────────────────────────────────────

def test_gate_keeps_what_the_model_names_and_logs_every_decision(tmp_path):
    items = [candidate(n) for n in range(12)]
    model = FakeModel("2 5", "0")

    result = run_gate(items, PROFILE, model, batch_size=10, run_id=7, log_root=tmp_path)

    assert [c.external_id for c in result.kept] == [items[1].external_id,
                                                    items[4].external_id]
    assert len(result.dropped) == 10
    assert (result.batches, result.failed_batches) == (2, 0)
    assert (result.input_tokens, result.output_tokens) == (200, 10)
    lines = [json.loads(line) for line in
             result.log_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 12
    assert [line["decision"] for line in lines].count("keep") == 2
    first = lines[0]
    assert first["client"] == "jt-express"
    assert first["profile_version"] == PROFILE.profile_version
    assert first["prompt_version"] == result.prompt_version
    assert first["run_id"] == 7
    assert first["label"] == "Temu senkt Preise"


def test_an_unusable_reply_is_retried_once(tmp_path):
    items = [candidate(n) for n in range(3)]
    model = FakeModel("I would keep the first one.", "1")

    result = run_gate(items, PROFILE, model, batch_size=10, log_root=tmp_path)

    assert len(model.calls) == 2
    assert [c.external_id for c in result.kept] == [items[0].external_id]
    assert result.failed_batches == 0


def test_a_batch_that_fails_twice_is_kept_whole(tmp_path):
    """Dropping is final for a title-only item, so failure falls towards keeping."""
    items = [candidate(n) for n in range(4)]
    model = FakeModel(TimeoutError("read timed out"), "")

    result = run_gate(items, PROFILE, model, batch_size=10, log_root=tmp_path)

    assert len(result.kept) == 4 and not result.dropped
    assert result.failed_batches == 1
    lines = [json.loads(line) for line in
             result.log_path.read_text(encoding="utf-8").splitlines()]
    assert all(line["fail_open"] and line["decision"] == "keep" for line in lines)


def test_a_config_error_aborts_instead_of_keeping_everything(tmp_path):
    model = FakeModel(GateConfigError("AuthenticationError: bad key"))
    with pytest.raises(GateConfigError):
        run_gate([candidate(1)], PROFILE, model, log_root=tmp_path)


def test_no_candidates_means_no_call_and_no_log(tmp_path):
    model = FakeModel()
    result = run_gate([], PROFILE, model, log_root=tmp_path)
    assert model.calls == [] and result.log_path is None


def test_decision_logs_are_pruned_after_keep_days(tmp_path):
    for name in ("2026-08-01.jsonl", "2026-09-05.jsonl", "notes.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")

    removed = prune_logs(tmp_path, keep_days=30, today=date(2026, 9, 11))

    assert [p.name for p in removed] == ["2026-08-01.jsonl"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-05.jsonl", "notes.jsonl"]


def test_logged_keeps_uses_the_latest_decision_per_url(tmp_path):
    log_dir = tmp_path / PROFILE.slug
    log_dir.mkdir()
    first = [
        {"client": PROFILE.slug, "source": "news.test", "external_id": "a",
         "decision": "keep"},
        {"client": PROFILE.slug, "source": "news.test", "external_id": "b",
         "decision": "drop"},
    ]
    second = [
        {"client": PROFILE.slug, "source": "news.test", "external_id": "a",
         "decision": "drop"},
        {"client": PROFILE.slug, "source": "news.test", "external_id": "b",
         "decision": "keep"},
    ]
    (log_dir / "2026-09-10.jsonl").write_text(
        "\n".join(json.dumps(row) for row in first) + "\n", encoding="utf-8")
    (log_dir / "2026-09-11.jsonl").write_text(
        "\n".join(json.dumps(row) for row in second) + "\n", encoding="utf-8")

    assert [(row["source"], row["external_id"]) for row in
            logged_keeps(PROFILE.slug, tmp_path)] == [("news.test", "b")]


def test_a_malformed_decision_log_cannot_silently_lose_a_keep(tmp_path):
    log_dir = tmp_path / PROFILE.slug
    log_dir.mkdir()
    (log_dir / "2026-09-11.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid title-gate JSONL"):
        logged_keeps(PROFILE.slug, tmp_path)
