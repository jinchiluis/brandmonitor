"""Alert gate: news-only eligibility, binary decisions, and one-email batching."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alert_gate import (  # noqa: E402
    PUSH_CAP, PUSH_SUMMARY_BYTES, AlertDeliveryError, PendingAlert, build_email,
    build_pushes, clip_utf8, eligible_news_items, parse_reply, run_alert_gate,
)
from src.db import migrate, session  # noqa: E402
from src.profile import load_profile  # noqa: E402


PROFILE = load_profile("jt-express")
START = "2026-09-12T05:00:00+00:00"
NOW = "2026-09-12T07:00:00+00:00"


class FakeCaller:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value, ensure_ascii=False), {"in": 100, "out": 20}


class FakeSender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, alerts):
        self.calls.append(list(alerts))
        if self.error:
            raise self.error
        return "<digest@test>"


@pytest.fixture
def corpus(tmp_path):
    db = tmp_path / "alerts.sqlite3"
    migrate(db)
    with session(db) as conn:
        conn.execute(
            "INSERT INTO run (kind,started_at,finished_at,status,window_start,window_end) "
            "VALUES ('news',?,?, 'ok',?,?)", (START, START, START, NOW))
    return db


def add_item(conn, raw_id, title, body, *, kind="news", slug="example.de",
             fetched="2026-09-12T06:00:00+00:00"):
    url = f"https://{slug}/{raw_id}"
    payload = {"body_status": "ok", "body_text": body, "title": title, "url": url}
    conn.execute(
        "INSERT INTO raw_item (id,source_slug,source_kind,external_id,version,url,title,"
        "published_at,fetched_at,content_hash,payload) VALUES (?,?,?,?,1,?,?,NULL,?,?,?)",
        (raw_id, slug, kind, url, url, title, fetched, f"hash-{raw_id}",
         json.dumps(payload, ensure_ascii=False)))
    return url


def add_body_gate(conn, raw_id, relevant, *, reasons=("brand:Temu",),
                  created="2026-09-12T06:30:00+00:00"):
    verdict = "unsure" if relevant is None else "relevant" if relevant else "irrelevant"
    conn.execute(
        "INSERT INTO assessment (raw_item_id,client_slug,prompt_version,profile_version,"
        "created_at,relevant,payload) VALUES (?,?,?,?,?,?,?)",
        (raw_id, PROFILE.slug, "body_gate-news-test", PROFILE.profile_version, created,
         relevant, json.dumps({"stage": "body_gate", "verdict": verdict,
                               "selector_reasons": list(reasons)})))


def add_title_route(conn, raw_id, url, *, reasons=("brand:J&T",)):
    route = {"client": PROFILE.slug, "at": "2026-09-12T05:30:00+00:00",
             "reasons": list(reasons), "decision": "keep"}
    hint = {"url": url, "title_gate_routes": {PROFILE.slug: route}}
    conn.execute(
        "INSERT INTO body_fetch (source_slug,external_id,discovery_hash,hint_payload,status) "
        "VALUES ('example.de',?,?,?,'ok')", (url, f"discovery-{raw_id}", json.dumps(hint)))


def positive(summary="这是一个需要人工查看的潜在预警。"):  # model response
    return {"potential_alert": True, "summary_zh": summary}


def negative():
    return {"potential_alert": False, "summary_zh": ""}


def test_eligibility_is_news_relevant_and_unsure_plus_title_route(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)
        add_item(conn, 2, "Temu strike", "A strike may affect Temu deliveries.")
        add_body_gate(conn, 2, None)
        add_item(conn, 3, "Temu fire", "A fire affected a Temu supplier.")
        add_body_gate(conn, 3, 0)
        url = add_item(conn, 4, "J&T opens a depot", "J&T opened a German depot.")
        add_title_route(conn, 4, url)
        add_item(conn, 5, "Official fine", "A regulator imposed a fine.", kind="regulatory")
        add_body_gate(conn, 5, 1)

    items = eligible_news_items(corpus, PROFILE, START, NOW)

    assert {item.raw_item_id for item in items} == {1, 2, 4}
    assert {item.raw_item_id: item.route for item in items}[4] == "title_gate_body"


def test_all_positives_are_sent_in_one_email_and_not_repeated(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)
        add_item(conn, 2, "Temu strike", "A strike may affect Temu deliveries.")
        add_body_gate(conn, 2, None)
        url = add_item(conn, 3, "J&T mention", "J&T announced a new partnership.")
        add_title_route(conn, 3, url)
    caller = FakeCaller(positive("Temu可能面临罚款。"), positive("罢工可能影响配送。"),
                        positive("报道提及极兔的新合作。"))
    sender = FakeSender()

    result = run_alert_gate(PROFILE, db_path=corpus, caller=caller, sender=sender, now=NOW)

    assert len(result.offered) == len(result.decisions) == 3
    assert result.positives == result.emailed == 3
    assert len(sender.calls) == 1 and len(sender.calls[0]) == 3
    with session(corpus) as conn:
        rows = list(conn.execute("SELECT * FROM alert_decision"))
        watermark = conn.execute(
            "SELECT position FROM watermark WHERE scope='analysis:jt-express:alert-news'"
        ).fetchone()["position"]
    assert len(rows) == 3 and all(row["sent_at"] for row in rows)
    assert watermark == NOW

    again_caller, again_sender = FakeCaller(), FakeSender()
    again = run_alert_gate(
        PROFILE, db_path=corpus, caller=again_caller, sender=again_sender,
        now="2026-09-12T08:00:00+00:00")
    assert again.decisions == [] and again.emailed == 0
    assert again_caller.calls == [] and again_sender.calls == []


def test_negative_is_recorded_without_email(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine explainer",
                 "A generic guide says a Temu seller may face a fine.")
        add_body_gate(conn, 1, 1)
    sender = FakeSender()

    result = run_alert_gate(
        PROFILE, db_path=corpus, caller=FakeCaller(negative()), sender=sender, now=NOW)

    assert result.positives == result.emailed == 0 and sender.calls == []
    with session(corpus) as conn:
        row = conn.execute("SELECT potential_alert,sent_at FROM alert_decision").fetchone()
    assert row["potential_alert"] == 0 and row["sent_at"] is None


def test_article_without_own_brand_or_alert_term_never_calls_model(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu expands", "Temu opened another logistics centre.")
        add_body_gate(conn, 1, 1)
    caller = FakeCaller()

    result = run_alert_gate(
        PROFILE, db_path=corpus, caller=caller, sender=FakeSender(), now=NOW)

    assert result.eligible == 1 and result.offered == [] and caller.calls == []


def test_failed_email_is_retried_without_repeating_model_call(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)

    with pytest.raises(AlertDeliveryError):
        run_alert_gate(
            PROFILE, db_path=corpus, caller=FakeCaller(positive()),
            sender=FakeSender(AlertDeliveryError("mail down")), now=NOW)
    with session(corpus) as conn:
        row = conn.execute("SELECT potential_alert,sent_at FROM alert_decision").fetchone()
    assert row["potential_alert"] == 1 and row["sent_at"] is None

    caller, sender = FakeCaller(), FakeSender()
    result = run_alert_gate(
        PROFILE, db_path=corpus, caller=caller, sender=sender,
        now="2026-09-12T08:00:00+00:00")
    assert caller.calls == []
    assert result.emailed == 1 and len(sender.calls) == 1


def test_dry_run_changes_nothing(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)

    result = run_alert_gate(PROFILE, db_path=corpus, now=NOW, dry_run=True)

    assert len(result.offered) == 1 and result.decisions == []
    with session(corpus) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM alert_decision").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT 1 FROM watermark WHERE scope='analysis:jt-express:alert-news'"
        ).fetchone() is None


@pytest.mark.parametrize("reply, expected", [
    (json.dumps(positive("摘要"), ensure_ascii=False), (True, "摘要")),
    (json.dumps(negative()), (False, "")),
    ('{"potential_alert": true, "summary_zh": ""}', None),
    ('{"potential_alert": false, "summary_zh": "not empty"}', None),
    ('{"potential_alert": 1, "summary_zh": "x"}', None),
])
def test_reply_shape(reply, expected):
    assert parse_reply(reply) == expected


def test_email_contains_every_alert_in_one_message():
    from src.alert_gate import PendingAlert

    alerts = [
        PendingAlert((1,), "one.de", "one", "https://one.de/a", "First", "第一条摘要。", "J&T Express Germany"),
        PendingAlert((2,), "two.de", "two", "https://two.de/a", "Second", "第二条摘要。", "J&T Express Germany"),
    ]
    message = build_email(
        alerts, sender="sender@example.com", recipient="reviewer@example.com")
    plain = message.get_body(preferencelist=("plain",)).get_content()

    assert message["To"] == "reviewer@example.com"
    assert "2 potential alerts" in message["Subject"]
    assert "第一条摘要。" in plain and "https://two.de/a" in plain
    assert "[J&T Express Germany] First" in plain


def test_source_does_not_look_like_a_bare_domain_to_mail_clients():
    from src.alert_gate import PendingAlert

    alerts = [PendingAlert(
        (1,), "example.de", "one", "https://example.de/a", "First",
        "摘要。", "J&T Express Germany")]
    message = build_email(
        alerts, sender="sender@example.com", recipient="reviewer@example.com")
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html_body = message.get_body(preferencelist=("html",)).get_content()

    # The source is still readable as "example.de" but must not contain an
    # unbroken "word.word" substring, which is what mail clients' automatic
    # data detectors (Apple Mail, Outlook, Gmail...) match on to add their own
    # link — only the explicit URL link (still a bare "example.de" elsewhere,
    # inside the URL) should navigate.
    assert "Source: example.de" not in plain and "Source: example​.​de" in plain
    assert "Source:</strong> example.de<" not in html_body
    assert "Source:</strong> example​.​de<" in html_body
    assert "https://example.de/a" in plain and "https://example.de/a" in html_body



def test_each_emailed_alert_is_pushed_after_the_email(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)
        add_item(conn, 2, "Temu strike", "A strike may affect Temu deliveries.")
        add_body_gate(conn, 2, None)
    pushed = []

    def notifier(alerts):
        pushed.extend(alerts)
        return len(alerts)

    result = run_alert_gate(
        PROFILE, db_path=corpus, caller=FakeCaller(positive(), positive()),
        sender=FakeSender(), notifier=notifier, now=NOW)

    assert len(pushed) == result.pushed == 2
    assert {alert.title for alert in pushed} == {"Temu fine", "Temu strike"}


def test_failed_push_leaves_alerts_marked_sent(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)

    result = run_alert_gate(
        PROFILE, db_path=corpus, caller=FakeCaller(positive()),
        sender=FakeSender(), notifier=lambda alerts: 0, now=NOW)

    assert result.emailed == 1 and result.pushed == 0
    with session(corpus) as conn:
        assert conn.execute("SELECT sent_at FROM alert_decision").fetchone()["sent_at"]


def test_no_push_without_an_email(corpus):
    with session(corpus) as conn:
        add_item(conn, 1, "Temu fine", "Temu faces a regulatory fine.")
        add_body_gate(conn, 1, 1)
    pushed = []

    with pytest.raises(AlertDeliveryError):
        run_alert_gate(
            PROFILE, db_path=corpus, caller=FakeCaller(positive()),
            sender=FakeSender(AlertDeliveryError("mail down")),
            notifier=lambda alerts: pushed.extend(alerts) or 0, now=NOW)
    assert pushed == []


def pending(number, summary="极兔据报道面临罚款。"):
    return PendingAlert((number,), "dvz", f"x{number}", f"https://example.de/{number}",
                        f"Title {number}", summary, "J&T Express Germany")


def test_push_carries_summary_and_a_linkified_url():
    [push] = build_pushes([pending(1)])

    assert push["title"] == "[J&T Express Germany] Title 1"
    assert push["message"] == "极兔据报道面临罚款。\n\ndvz\nhttps://example.de/1"
    assert "actions" not in push


def test_push_source_does_not_look_like_a_bare_domain_to_ntfy():
    alert = PendingAlert(
        (1,), "example.de", "one", "https://example.de/a", "Title 1",
        "摘要。", "J&T Express Germany")
    [push] = build_pushes([alert])

    # The source line itself must not be an unbroken "word.word" substring
    # (what ntfy's own auto-linker matches on); the URL line still is one,
    # and should be — that's the one link meant to navigate.
    assert "\nexample.de\n" not in push["message"]
    assert "\nexample​.​de\n" in push["message"]
    assert "https://example.de/a" in push["message"]


def test_long_chinese_summary_is_clipped_on_a_character_boundary():
    clipped = clip_utf8("极" * 2000, PUSH_SUMMARY_BYTES)

    assert len(clipped.encode("utf-8")) <= PUSH_SUMMARY_BYTES
    assert clipped.endswith("…") and set(clipped[:-1]) == {"极"}
    [push] = build_pushes([pending(1, "极" * 2000)])
    assert len(json.dumps(push, ensure_ascii=False).encode("utf-8")) < 4096


def test_pushes_are_capped_with_one_overflow_notice():
    pushes = build_pushes([pending(n) for n in range(1, PUSH_CAP + 4)])

    assert len(pushes) == PUSH_CAP + 1
    assert pushes[-1]["title"] == "+3 more potential alerts" and "actions" not in pushes[-1]
