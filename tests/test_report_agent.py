"""Tests for the report stack. No model and no network: the assessor is a fake.

The three deterministic stages are the point here. Stage 2's judgment cannot be
unit-tested, but everything that surrounds it can: what the export is allowed to
call a date, that the tool surface refuses an id it did not freeze, that the
ledger is a bijection, that a report link resolves only through frozen evidence,
and that the verifier fails when any of those is broken.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import migrate, session, utcnow  # noqa: E402
from src.report_agent import assess as A  # noqa: E402
from src.report_agent.bundle import Bundle, previous_bundle, window_bounds  # noqa: E402
from src.report_agent.export import _date_info, _period, export_window  # noqa: E402
from src.report_agent.render import RenderError, render_bundle  # noqa: E402
from src.report_agent.schema import decision, review_depth  # noqa: E402
from src.report_agent.tools import BundleTools, ToolError, ToolLog  # noqa: E402
from src.report_agent.verify import verify_bundle  # noqa: E402


# ── the window ────────────────────────────────────────────────────────────

def test_window_is_local_days_stored_as_utc():
    start, end = window_bounds(date(2026, 9, 5), date(2026, 9, 11))
    # Berlin is UTC+2 in September, and `until` is inclusive, so the exclusive
    # bound is midnight after the 11th.
    assert start.startswith("2026-09-04T22:00")
    assert end.startswith("2026-09-11T22:00")


def test_window_rejects_a_backwards_range():
    with pytest.raises(ValueError):
        window_bounds(date(2026, 9, 11), date(2026, 9, 5))


# ── dates ─────────────────────────────────────────────────────────────────

def test_lastmod_never_becomes_a_publication_date():
    row = {"source_kind": "news", "published_at": "2026-09-08T00:00:00Z"}
    day, provenance = _date_info(row, {"published_at_source": "lastmod"})
    assert day is None and provenance == "lastmod"


def test_a_page_date_is_kept():
    row = {"source_kind": "news", "published_at": "2026-09-08T06:00:00+02:00"}
    assert _date_info(row, {"published_at_source": "page"}) == ("2026-09-08", "page")


def test_a_dsa_row_is_dated_by_submission_and_labelled_as_such():
    day, provenance = _date_info({"source_kind": "dsa"}, {"received_date": "2026-08-12"})
    assert (day, provenance) == ("2026-08-12", "submission_date_not_event")


def test_periods_split_around_the_window():
    assert _period(None, "2026-09-05", "2026-09-11") == "undated"
    assert _period("2026-09-01", "2026-09-05", "2026-09-11") == "history"
    assert _period("2026-09-07", "2026-09-05", "2026-09-11") == "week"
    assert _period("2026-09-20", "2026-09-05", "2026-09-11") == "future"


# ── a small end-to-end bundle ─────────────────────────────────────────────

WEEK = (date(2026, 9, 5), date(2026, 9, 11))


def _store(conn, raw_id, *, slug="example.de", kind="news", title="T", url=None,
           published="2026-09-07T08:00:00+02:00", fetched="2026-09-08T05:00:00+00:00",
           source="page", body="Ein Paketdienst liefert für eine Plattform."):
    payload = {"url": url or f"https://{slug}/a/{raw_id}", "title": title,
               "published_at": published, "published_at_source": source,
               "discovered_via": "sitemap", "body_status": "ok", "body_text": body}
    conn.execute(
        "INSERT INTO raw_item (id, source_slug, source_kind, external_id, version, url, "
        "title, published_at, fetched_at, content_hash, payload) "
        "VALUES (?,?,?,?,1,?,?,?,?,?,?)",
        (raw_id, slug, kind, payload["url"], payload["url"], title, published, fetched,
         f"hash{raw_id}", json.dumps(payload)))
    return raw_id


def _gate(conn, raw_id, relevant, reason="because"):
    conn.execute(
        "INSERT INTO assessment (raw_item_id, client_slug, prompt_version, "
        "profile_version, created_at, relevant, payload) VALUES (?,?,?,?,?,?,?)",
        (raw_id, "jt-express", "body_gate-news-test", "test", utcnow(), relevant,
         json.dumps({"stage": "body_gate", "verdict": "relevant" if relevant else
                     "irrelevant", "reason": reason, "selector_reasons": ["topic:GPSR"]})))


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Two candidates and one stopped item, exported into a real bundle."""
    db = tmp_path / "report.sqlite3"
    migrate(db)
    with session(db) as conn:
        _store(conn, 101, title="Zollfreigrenze fällt", body="Der Zoll ändert Regeln.")
        _store(conn, 102, title="Hafenstreik in Hamburg", body="Warnstreik im Hafen.")
        _store(conn, 103, title="Wetterbericht", body="Es bleibt sonnig.")
        # A restamped archive page: lastmod only, so it must end up undated.
        _store(conn, 104, title="Altes Dossier", source="lastmod",
               published="2026-09-10T00:00:00Z", body="Archivseite.")
        for raw_id in (101, 102, 104):
            _gate(conn, raw_id, 1)
        _gate(conn, 103, 0, "weather, not the client's market")

    out = tmp_path / "bundle"
    monkeypatch.setattr("src.report_agent.bundle.REPORTS_DIR", tmp_path / "reports")
    return export_window("jt-express", *WEEK, out_dir=out, db_path=db)


def test_export_splits_candidates_from_stopped_items(bundle):
    assert {row["id"] for row in bundle.evidence} == {101, 102, 104}
    assert [row["id"] for row in bundle.stopped] == [103]
    assert bundle.manifest["ledger_identities"] == 4


def test_export_records_the_inputs_that_shaped_it(bundle):
    hashes = bundle.manifest["input_hashes"]
    assert "clients/jt-express/profile.json" in hashes
    assert all(len(digest) == 64 for digest in hashes.values())


def test_a_restamped_page_is_undated_rather_than_dated_wrongly(bundle):
    row = next(r for r in bundle.evidence if r["id"] == 104)
    assert row["event_or_publication_day"] is None
    assert row["period"] == "undated"


def test_census_reconciles_in_the_manifest(bundle):
    manifest = bundle.manifest
    passed = sum(row["n"] for row in manifest["counts"] if row["route"] == "body_gate")
    assert (passed + manifest["stopped_asof_identities"]
            + manifest["gated_identities_without_pre_cutoff_raw"]
            == manifest["unique_gated_identities"])


# ── the tool surface ──────────────────────────────────────────────────────

def test_tools_refuse_an_id_the_export_does_not_contain(bundle):
    tools = BundleTools(bundle, ToolLog())
    with pytest.raises(ToolError):
        tools.get_item(999999)


def test_search_reaches_items_a_gate_stopped(bundle):
    tools = BundleTools(bundle, ToolLog())
    found = tools.search_titles("Wetterbericht", 10, True)
    assert [hit["raw_item_id"] for hit in found["results"]] == [103]
    assert found["results"][0]["in_export_as"] == "stopped"
    assert tools.search_titles("Wetterbericht", 10, False)["matched"] == 0


def test_depth_is_measured_from_the_calls_that_happened(bundle):
    log = ToolLog()
    tools = BundleTools(bundle, log)
    tools.search_titles("Hafenstreik", 5, True)
    row = bundle.by_id[102]
    assert review_depth(row, log.tools_for(102)) == "search_snippet"
    tools.get_body(102)
    assert review_depth(row, log.tools_for(102)) == "full_stored_body"
    assert review_depth(bundle.by_id[101], log.tools_for(101)) == "title_only"


# ── decisions ─────────────────────────────────────────────────────────────

def test_a_rule_cannot_write_a_treatment_that_needs_judgment():
    with pytest.raises(ValueError):
        decision(1, "report", "r", decided_by="rule", step="rule")


def test_an_unknown_treatment_is_refused():
    with pytest.raises(ValueError):
        decision(1, "looks_fine", "r", decided_by="assessor", step="deep_read")


def _assess_scripted(bundle, *, status="reportable"):
    """Stage 2's assembly with the model's three replies supplied by hand."""
    log = ToolLog()
    BundleTools(bundle, log, step="deep_read", story_id="s1").get_body(101)
    triage = {101: {"keep": True, "reason": "k", "bucket": "regulatory"},
              102: {"keep": True, "reason": "k", "bucket": "transport_disruption"},
              104: {"keep": False, "reason": "undated archive", "bucket": "other"}}
    stories = [{"story_id": "s1", "label": "Customs", "item_ids": [101, 102],
                "primary_id": 101, "carries_issue_id": "", "priority": "high",
                "merge_note": "second source"}]
    clustered = {"stories": stories, "unclustered": []}
    read = [{"story_id": "s1", "status": status, "headline": "Customs threshold",
             "what_happened": "W", "why_it_matters": "Y", "recommended_check": "C",
             "scope_limits": ["not adopted"], "excerpts": [], "cited_ids": [101],
             "next_trigger": "adoption", "use": "main", "challenge": {}}]
    carried = {"issues": [], "previous": {"issues": [], "source": None}}
    register = A._build_register(bundle, stories, read, carried)
    decisions = A._build_decisions(bundle, log, triage, clustered, stories, read, set())
    return register, decisions


def test_every_identity_gets_exactly_one_decision(bundle):
    _, decisions = _assess_scripted(bundle)
    ids = [entry["raw_item_id"] for entry in decisions]
    assert sorted(ids) == [101, 102, 103, 104]
    assert len(ids) == len(set(ids))


def test_the_cited_item_is_reported_and_the_second_source_is_merged(bundle):
    _, decisions = _assess_scripted(bundle)
    by_id = {entry["raw_item_id"]: entry for entry in decisions}
    assert by_id[101]["treatment"] == "report"
    assert by_id[101]["review_depth"] == "full_stored_body"
    assert by_id[102]["treatment"] == "merge"
    assert by_id[103]["treatment"] == "retain_gate_stop"
    assert by_id[103]["decided_by"] == "rule"
    assert by_id[104]["treatment"] == "omit"


def test_an_unsupported_story_marks_every_member_insufficient(bundle):
    _, decisions = _assess_scripted(bundle, status="insufficient_evidence")
    by_id = {entry["raw_item_id"]: entry for entry in decisions}
    assert by_id[101]["treatment"] == by_id[102]["treatment"] == "insufficient_evidence"


def test_the_register_carries_evidence_and_a_next_trigger(bundle):
    register, _ = _assess_scripted(bundle)
    issue = register[0]
    assert issue["current"] == [101, 102]
    assert issue["next"] == "adoption"
    assert {e["raw_item_id"] for e in issue["evidence"]} == {101, 102}


# ── rendering ─────────────────────────────────────────────────────────────

def _draft(item_id=101):
    return {"title": "周报", "dateline": "2026-09-05 至 09-11", "scope_note": "测试。",
            "verdict": f"**结论。** 见[报道](item:{item_id})。",
            "sections": [{"heading": "关税", "story_ids": ["s1"],
                          "markdown": f"**发生了什么。** 见[报道](item:{item_id})。"}],
            "watchlist_markdown": "| 事项 | 状态 | 触发点 |\n|---|---|---|\n| 关税 | 待定 | 通过 |",
            "coverage_markdown": "覆盖说明。"}


def _rendered(bundle, draft=None):
    register, decisions = _assess_scripted(bundle)
    bundle.write("issue-register.json", register)
    bundle.write("decisions.json", decisions)
    bundle.write("report-draft.json", draft or _draft())
    return render_bundle(bundle)


def test_a_heading_the_writer_repeated_is_not_rendered_twice(bundle):
    draft = _draft()
    draft["coverage_markdown"] = "## 覆盖与证据限制\n\n覆盖说明。"
    _rendered(bundle, draft)
    report = (bundle.path / "weekly-report.zh.md").read_text(encoding="utf-8")
    assert report.count("## 覆盖与证据限制") == 1


def test_the_cutoff_is_shown_in_the_clients_own_timezone(bundle):
    # Stored as 22:00 UTC so it compares with fetched_at; midnight in Berlin.
    assert bundle.window_end.startswith("2026-09-11T22:00")
    assert bundle.display_cutoff == "2026-09-12 00:00 Europe/Berlin"


def test_an_unreadable_title_is_an_identity_like_any_other(bundle):
    # The report may say "we saw this and could not read it", so the id has to
    # resolve; what it may not do is claim what the page said.
    assert bundle.manifest["ledger_identities"] == (
        len(bundle.evidence) + len(bundle.stopped) + len(bundle.unavailable))
    assert all(row["in_export_as"] == "candidate" for row in bundle.evidence)
    assert all(row["in_export_as"] == "stopped" for row in bundle.stopped)


def test_a_report_link_resolves_to_the_frozen_url(bundle):
    result = _rendered(bundle)
    assert result.cited_ids == {101}
    report = (bundle.path / "weekly-report.zh.md").read_text(encoding="utf-8")
    assert bundle.by_id[101]["url"] in report


def test_a_link_to_an_id_outside_the_export_fails_the_build(bundle):
    with pytest.raises(RenderError, match="not in the frozen export"):
        _rendered(bundle, _draft(item_id=999999))


def test_a_literal_url_that_is_not_frozen_evidence_fails_the_build(bundle):
    draft = _draft()
    draft["verdict"] = "见[外部](https://example.invalid/made-up)。"
    with pytest.raises(RenderError, match="not in the frozen evidence"):
        _rendered(bundle, draft)


def test_the_ledger_refuses_a_missing_decision(bundle):
    register, decisions = _assess_scripted(bundle)
    bundle.write("issue-register.json", register)
    bundle.write("decisions.json", [d for d in decisions if d["raw_item_id"] != 103])
    bundle.write("report-draft.json", _draft())
    with pytest.raises(RenderError, match="no decision"):
        render_bundle(bundle)


# ── verification ──────────────────────────────────────────────────────────

def test_a_rendered_bundle_verifies(bundle):
    _rendered(bundle)
    result = verify_bundle(bundle)
    assert result["status"] == "passed", result["errors"]
    assert all(result["checks"].values())
    assert result["ledger_rows"] == 4


def test_verification_fails_when_a_decision_is_duplicated(bundle):
    _rendered(bundle)
    decisions = bundle.load("decisions.json")
    bundle.write("decisions.json", decisions + [decisions[0]])
    result = verify_bundle(bundle)
    assert result["status"] == "failed"
    assert any("decided twice" in error for error in result["errors"])


def test_verification_warns_when_the_report_cites_something_unread(bundle):
    _rendered(bundle, _draft(item_id=102))
    result = verify_bundle(bundle)
    # 102 was merged, never opened. Worth seeing every week; not a build failure.
    assert result["status"] == "passed"
    assert any("never opened" in warning for warning in result["warnings"])


def test_bundle_hashes_cover_every_file(bundle):
    _rendered(bundle)
    verify_bundle(bundle)
    hashes = bundle.load("bundle-hashes.json")
    assert "evidence.json" in hashes and "weekly-report.zh.html" in hashes
    assert "bundle-hashes.json" not in hashes


# ── the cycle before this one ─────────────────────────────────────────────

def test_the_previous_cycle_is_the_latest_one_that_closed_before_this_window(tmp_path):
    reports = tmp_path / "reports"
    for name, end in (("c1", "2026-08-29T22:00:00+00:00"),
                      ("c2", "2026-09-04T22:00:00+00:00"),
                      ("c3", "2026-09-11T22:00:00+00:00")):
        directory = reports / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps(
            {"client": "jt-express", "window_end_exclusive": end}), encoding="utf-8")
        (directory / "issue-register.json").write_text("[]", encoding="utf-8")

    earlier = previous_bundle("jt-express", "2026-09-04T22:00:00+00:00", reports)
    assert earlier is not None and earlier.path.name == "c2"
    # c3 closes after this window opens, so it is this cycle, not the last one.
    assert previous_bundle("other-client", "2026-09-04T22:00:00+00:00", reports) is None


def test_a_first_cycle_has_no_open_issues(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr("src.report_agent.tools.previous_bundle",
                        lambda *a, **k: None)
    tools = BundleTools(bundle, ToolLog())
    issues = tools.open_issues()
    assert issues["issues"] == [] and issues["source"] is None
    assert "first cycle" in issues["note"]


def test_an_earlier_register_reaches_the_assessor(bundle, tmp_path, monkeypatch):
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    (earlier / "manifest.json").write_text(json.dumps(
        {"client": "jt-express", "window_end_exclusive": "2026-09-04T22:00:00+00:00"}),
        encoding="utf-8")
    (earlier / "issue-register.json").write_text(json.dumps(
        [{"id": "ports", "label": "Port labour dispute", "use": "conditional_watch",
          "status": "Warning strike reported 17 August", "next": "Terminal status",
          "current": [24618]}]), encoding="utf-8")
    monkeypatch.setattr("src.report_agent.tools.previous_bundle",
                        lambda *a, **k: Bundle(earlier))
    issues = BundleTools(bundle, ToolLog()).open_issues()
    assert [issue["id"] for issue in issues["issues"]] == ["ports"]
    assert issues["source"] == "earlier"
