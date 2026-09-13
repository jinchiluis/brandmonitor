import json
from datetime import datetime, timedelta, timezone

import pytest

import health.check as check
from health.check import (
    HealthStatus,
    MarkerError,
    Probe,
    QualityProbe,
    evaluate,
    notification_action,
    parse_marker,
    parse_quality_snapshot,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def marker_payload(*, finished: datetime, worst: int = 0, news: int = 0,
                   cycle_date: str | None = None) -> str:
    payload = {
        "finished_utc": finished.isoformat().replace("+00:00", "Z"),
        "worst_exit": worst,
        "stages": {"news": news, "backup": 0},
        "log": "data/log/2026-09-12/run_daily.txt",
    }
    if cycle_date:
        payload["cycle_date"] = cycle_date
    return json.dumps(
        payload
    )


def quality_payload(*, generated: datetime, status: str = "healthy",
                    cycle_date: str = "2026-09-12",
                    incidents: list[dict] | None = None,
                    incident_key: str | None = None) -> str:
    return json.dumps({
        "schema_version": 1,
        "kind": "coverage_health",
        "generated_utc": generated.isoformat().replace("+00:00", "Z"),
        "cycle_date": cycle_date,
        "status": status,
        "incident_key": incident_key,
        "incidents": incidents or [],
    })


def test_fresh_success_is_healthy():
    probe = Probe(parse_marker(marker_payload(finished=NOW - timedelta(hours=2))))

    status = evaluate(probe, checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "healthy"
    assert not status.alert


def test_staleness_precedes_exit_code():
    probe = Probe(
        parse_marker(
            marker_payload(finished=NOW - timedelta(hours=27), worst=2, news=2)
        )
    )

    status = evaluate(probe, checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "stale"
    assert status.alert


def test_fresh_nonzero_stage_alerts():
    probe = Probe(
        parse_marker(marker_payload(finished=NOW - timedelta(hours=1), worst=1, news=1))
    )

    status = evaluate(probe, checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "run_failed"
    assert "news=1" in status.details[1]


def test_unreachable_uses_fresh_cached_marker_as_grace():
    status = evaluate(
        Probe(None, "connection timed out"),
        checked_at=NOW,
        stale_after=timedelta(hours=26),
        cached_finished_utc=(NOW - timedelta(hours=4)).isoformat(),
    )

    assert status.kind == "unreachable_grace"
    assert not status.alert


def test_unreachable_alerts_when_cached_marker_is_stale():
    status = evaluate(
        Probe(None, "connection timed out"),
        checked_at=NOW,
        stale_after=timedelta(hours=26),
        cached_finished_utc=(NOW - timedelta(hours=27)).isoformat(),
    )

    assert status.kind == "unreachable"
    assert status.alert


def test_invalid_marker_alerts_even_with_fresh_cache():
    status = evaluate(
        Probe(None, "marker is not valid JSON", invalid_marker=True),
        checked_at=NOW,
        stale_after=timedelta(hours=26),
        cached_finished_utc=(NOW - timedelta(hours=1)).isoformat(),
    )

    assert status.kind == "invalid_marker"
    assert status.alert


def test_invalid_worst_exit_is_rejected():
    with pytest.raises(MarkerError, match="worst_exit"):
        parse_marker(marker_payload(finished=NOW, worst=0, news=1))


def test_notifications_are_latched_and_recover_once():
    alert = HealthStatus("stale", "stale", (), True)
    healthy = HealthStatus("healthy", "healthy", (), False)

    assert notification_action(alert, {}) == "alert"
    assert notification_action(alert, {"notified_kind": "stale"}) is None
    assert notification_action(healthy, {"notified_kind": "stale"}) == "recovery"
    assert notification_action(healthy, {}) is None


def test_quality_warning_turns_a_green_run_into_an_alert():
    marker = Probe(parse_marker(marker_payload(
        finished=NOW - timedelta(minutes=3), cycle_date="2026-09-12"
    )))
    quality = QualityProbe(parse_quality_snapshot(quality_payload(
        generated=NOW - timedelta(minutes=4),
        status="warning",
        incident_key="source-a",
        incidents=[{
            "source": "a.de",
            "check": "zero_streak",
            "severity": "warning",
            "message": "zero twice",
        }],
    )))

    status = evaluate(
        marker,
        quality_probe=quality,
        checked_at=NOW,
        stale_after=timedelta(hours=26),
    )

    assert status.kind == "quality_warning" and status.alert
    assert status.incident_key == "source-a"
    assert "a.de" in status.details[1]


def test_new_quality_file_before_new_marker_is_a_non_alerting_transition():
    marker = Probe(parse_marker(marker_payload(
        finished=NOW - timedelta(hours=20), cycle_date="2026-09-11"
    )))
    quality = QualityProbe(parse_quality_snapshot(quality_payload(
        generated=NOW - timedelta(minutes=1), cycle_date="2026-09-12"
    )))

    status = evaluate(
        marker,
        quality_probe=quality,
        checked_at=NOW,
        stale_after=timedelta(hours=26),
    )

    assert status.kind == "quality_transition" and not status.alert


def test_changed_quality_incident_set_sends_an_updated_alert():
    first = HealthStatus("quality_warning", "warning", (), True, "a")
    changed = HealthStatus("quality_warning", "warning", (), True, "a-and-b")

    assert notification_action(first, {"notified_kind": "quality_warning",
                                       "notified_key": "a"}) is None
    assert notification_action(changed, {"notified_kind": "quality_warning",
                                         "notified_key": "a"}) == "alert"


def test_warning_quality_requires_an_incident():
    with pytest.raises(MarkerError, match="needs at least one incident"):
        parse_quality_snapshot(quality_payload(generated=NOW, status="warning"))


def push_args(tmp_path, *, topic: str | None = "bm-test-topic"):
    push_env = tmp_path / "push.env"
    if topic:
        push_env.write_text(f"NTFY_TOPIC={topic}\n", encoding="utf-8")
    smtp_env = tmp_path / "smtp.env"
    smtp_env.write_text("SMTP_PASSWORD=secret\n", encoding="utf-8")
    return check.build_parser().parse_args(
        ["--env-file", str(smtp_env), "--push-env-file", str(push_env)])


def test_push_is_off_without_a_topic(tmp_path, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert check.push_settings(tmp_path / "missing.env") is None
    settings_file = tmp_path / "push.env"
    settings_file.write_text("NTFY_TOPIC=abc\nNTFY_SERVER=https://ntfy.example/\n",
                             encoding="utf-8")
    assert check.push_settings(settings_file) == check.PushSettings(
        "https://ntfy.example", "abc", None)


def test_one_delivered_channel_latches_the_incident(tmp_path, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    pushes = []
    monkeypatch.setattr(check, "send_email", lambda **_: False)
    monkeypatch.setattr(check, "send_push",
                        lambda settings, **kw: pushes.append(kw) or True)
    args = push_args(tmp_path)
    assert check.deliver(args, subject="s", paragraphs=["p"], priority=5, tags=[])
    assert pushes[0]["priority"] == 5

    monkeypatch.setattr(check, "send_push", lambda settings, **kw: False)
    assert not check.deliver(args, subject="s", paragraphs=["p"], priority=5, tags=[])


def test_email_only_behaviour_is_unchanged_without_push(tmp_path, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(check, "send_email", lambda **_: True)
    monkeypatch.setattr(check, "send_push",
                        lambda *a, **k: pytest.fail("push must not be attempted"))
    args = push_args(tmp_path, topic=None)
    assert check.deliver(args, subject="s", paragraphs=["p"], priority=5, tags=[])


def test_push_priority_ranks_pipeline_incidents_above_coverage_warnings():
    def status(kind):
        return HealthStatus(kind, "t", (), True)
    assert check.push_priority("alert", status("stale")) == 5
    assert check.push_priority("alert", status("quality_critical")) == 4
    assert check.push_priority("alert", status("quality_warning")) == 3
    assert check.push_priority("recovery", status("healthy")) == 2
