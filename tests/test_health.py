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


def _daily_and_intraday(*, intraday_finished: datetime, intraday_worst: int):
    daily = Probe(parse_marker(marker_payload(finished=NOW - timedelta(hours=6))))
    intraday = Probe(parse_marker(marker_payload(
        finished=intraday_finished, worst=intraday_worst, news=intraday_worst)))
    return daily, intraday


def test_intraday_failure_after_the_daily_run_alerts():
    daily, intraday = _daily_and_intraday(
        intraday_finished=NOW - timedelta(hours=2), intraday_worst=2)

    status = evaluate(daily, checked_at=NOW, stale_after=timedelta(hours=26),
                      intraday_probe=intraday)

    assert status.kind == "intraday_failed"
    assert status.alert
    assert "news=2" in status.details[1]


def test_daily_run_supersedes_an_earlier_intraday_failure():
    daily, intraday = _daily_and_intraday(
        intraday_finished=NOW - timedelta(hours=14), intraday_worst=2)

    status = evaluate(daily, checked_at=NOW, stale_after=timedelta(hours=26),
                      intraday_probe=intraday)

    assert status.kind == "healthy"


def test_absent_intraday_marker_is_not_an_incident():
    daily = Probe(parse_marker(marker_payload(finished=NOW - timedelta(hours=2))))
    missing = Probe(None, "Cannot find path last_intraday_run.json")

    status = evaluate(daily, checked_at=NOW, stale_after=timedelta(hours=26),
                      intraday_probe=missing)

    assert status.kind == "healthy"


def test_daily_failure_outranks_intraday_failure():
    daily = Probe(parse_marker(
        marker_payload(finished=NOW - timedelta(hours=6), worst=1, news=1)))
    intraday = Probe(parse_marker(
        marker_payload(finished=NOW - timedelta(hours=1), worst=2, news=2)))

    status = evaluate(daily, checked_at=NOW, stale_after=timedelta(hours=26),
                      intraday_probe=intraday)

    assert status.kind == "run_failed"


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


def offline_payload(*, finished: datetime, log: str = "data/log/2026-09-12/run_daily.txt") -> str:
    """What the batch files write when netcheck says this host has no internet.

    One pseudo-stage, so the marker keeps the invariant that worst_exit equals
    the highest stage code.
    """
    return json.dumps({
        "finished_utc": finished.isoformat().replace("+00:00", "Z"),
        "worst_exit": 4,
        "cycle_date": "2026-09-12",
        "stages": {"netcheck": 4},
        "log": log,
    })


def test_an_offline_run_is_its_own_condition_not_a_stage_failure():
    """The email that started this: four stage codes describing one outage.

    Each said where that stage's first request happened to fail, which is an
    implementation detail of the stage rather than the cause.
    """
    probe = Probe(parse_marker(offline_payload(finished=NOW - timedelta(hours=6))))

    status = evaluate(probe, checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "offline"
    assert status.alert
    assert "no internet" in status.title
    assert any("run_daily.bat" in detail for detail in status.details),         "the regulatory half does not self-heal, so the email has to say so"


def test_an_offline_intraday_slot_says_there_is_nothing_to_do():
    daily = Probe(parse_marker(marker_payload(finished=NOW - timedelta(hours=6))))
    intraday = Probe(parse_marker(offline_payload(finished=NOW - timedelta(hours=2))))

    status = evaluate(daily, checked_at=NOW, stale_after=timedelta(hours=26),
                      intraday_probe=intraday)

    assert status.kind == "intraday_offline"
    assert any("nothing to do" in detail for detail in status.details)


def lost_network_payload(*, finished: datetime) -> str:
    """The marker after a run that started online and failed the check afterwards.

    Modelled on 2026-09-16: news collection completed normally, DNS stopped
    resolving five seconds later, and every stage after it failed on its own
    first request.
    """
    return json.dumps({
        "finished_utc": finished.isoformat().replace("+00:00", "Z"),
        "worst_exit": 4,
        "cycle_date": "2026-09-16",
        "stages": {"netcheck": 4, "news": 0, "title_gate": 0, "title_bodies": 1,
                   "safety_gate": 2, "dip": 1, "ep_procedures": 2, "backup": 0},
        "log": "data/log/2026-09-16/run_daily.txt",
    })


def test_a_run_that_lost_the_network_names_one_cause_not_four():
    probe = Probe(parse_marker(lost_network_payload(finished=NOW - timedelta(hours=6))))

    status = evaluate(probe, checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "offline_during_run"
    assert status.alert
    assert "lost the network" in status.title
    detail = " ".join(status.details)
    assert "safety_gate=2" in detail and "dip=1" in detail
    assert "netcheck" not in status.details[1], "netcheck is the diagnosis, not a casualty"
    assert "network failure rather than" in detail


def test_a_skipped_run_and_a_lost_network_are_different_alerts():
    """Both exit 4; only the stage list separates them."""
    skipped = evaluate(Probe(parse_marker(offline_payload(finished=NOW - timedelta(hours=2)))),
                       checked_at=NOW, stale_after=timedelta(hours=26))
    lost = evaluate(Probe(parse_marker(lost_network_payload(finished=NOW - timedelta(hours=2)))),
                    checked_at=NOW, stale_after=timedelta(hours=26))

    assert skipped.kind != lost.kind
    assert notification_action(lost, {"notified_kind": skipped.kind}) == "alert"


def test_a_clean_run_records_netcheck_without_raising_the_worst_exit():
    payload = json.loads(marker_payload(finished=NOW - timedelta(hours=2)))
    payload["stages"]["netcheck"] = 0

    status = evaluate(Probe(parse_marker(json.dumps(payload))),
                      checked_at=NOW, stale_after=timedelta(hours=26))

    assert status.kind == "healthy"


def test_offline_and_run_failed_latch_separately():
    """An outage during an ongoing incident is news, not a repeat of it."""
    offline = HealthStatus("offline", "offline", (), True)

    assert notification_action(offline, {"notified_kind": "run_failed"}) == "alert"
    assert notification_action(offline, {"notified_kind": "offline"}) is None


def test_a_stage_exit_above_the_contract_is_still_rejected():
    payload = json.loads(offline_payload(finished=NOW))
    payload["stages"] = {"netcheck": 5}
    payload["worst_exit"] = 5
    with pytest.raises(MarkerError, match="worst_exit"):
        parse_marker(json.dumps(payload))


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


def test_intraday_failure_includes_rejection_and_uses_its_stable_latch():
    daily, intraday = _daily_and_intraday(intraday_finished=NOW, intraday_worst=1)
    quality = QualityProbe(parse_quality_snapshot(quality_payload(
        generated=NOW, status="warning", incident_key="publisher-a-warning",
        incidents=[{"source": "a.de", "check": "publisher_rejection",
                    "severity": "warning", "message": "bodies: HTTP 429; paused for 23h"}])))
    status = evaluate(daily, intraday_probe=intraday, quality_probe=quality,
                      checked_at=NOW, stale_after=timedelta(hours=26))
    assert status.kind == "intraday_failed"
    assert any("a.de" in detail and "HTTP 429" in detail for detail in status.details)
    assert notification_action(status, {"notified_kind": "intraday_failed"}) == "alert"
    assert notification_action(status, {"notified_key": "publisher-a-warning"}) is None


def test_stale_rejection_snapshot_is_not_attached_to_a_new_failure():
    daily, intraday = _daily_and_intraday(intraday_finished=NOW, intraday_worst=2)
    quality = QualityProbe(parse_quality_snapshot(quality_payload(
        generated=NOW - timedelta(days=2), status="warning", incident_key="old-rejection",
        incidents=[{"source": "a.de", "check": "publisher_rejection",
                    "severity": "warning", "message": "old rejection"}])))
    status = evaluate(daily, intraday_probe=intraday, quality_probe=quality,
                      checked_at=NOW, stale_after=timedelta(hours=26))
    assert status.kind == "intraday_failed" and status.incident_key is None


def push_args(tmp_path, *, topic: str | None = "bm-test-topic"):
    push_env = tmp_path / "push.env"
    if topic:
        push_env.write_text(f"NTFY_HEALTH_TOPIC={topic}\n", encoding="utf-8")
    smtp_env = tmp_path / "smtp.env"
    smtp_env.write_text("SMTP_PASSWORD=secret\n", encoding="utf-8")
    return check.build_parser().parse_args(
        ["--env-file", str(smtp_env), "--push-env-file", str(push_env)])


def test_push_is_off_without_a_topic(tmp_path, monkeypatch):
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
    assert check.push_settings(tmp_path / "missing.env") is None
    settings_file = tmp_path / "push.env"
    settings_file.write_text("NTFY_HEALTH_TOPIC=abc\nNTFY_SERVER=https://ntfy.example/\n",
                             encoding="utf-8")
    assert check.push_settings(settings_file) == check.PushSettings(
        "https://ntfy.example", "abc", None)


def test_health_push_never_uses_the_admin_alert_topic(tmp_path, monkeypatch):
    # NTFY_TOPIC belongs to the laptop's news alerts; health must not borrow it.
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
    monkeypatch.setenv("NTFY_TOPIC", "admin-alerts")
    settings_file = tmp_path / "push.env"
    settings_file.write_text("NTFY_TOPIC=admin-alerts\n", encoding="utf-8")
    assert check.push_settings(settings_file) is None


def test_one_delivered_channel_latches_the_incident(tmp_path, monkeypatch):
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
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
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
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
