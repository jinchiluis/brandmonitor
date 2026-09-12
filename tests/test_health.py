import json
from datetime import datetime, timedelta, timezone

import pytest

from health.check import (
    HealthStatus,
    MarkerError,
    Probe,
    evaluate,
    notification_action,
    parse_marker,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def marker_payload(*, finished: datetime, worst: int = 0, news: int = 0) -> str:
    return json.dumps(
        {
            "finished_utc": finished.isoformat().replace("+00:00", "Z"),
            "worst_exit": worst,
            "stages": {"news": news, "backup": 0},
            "log": "data/log/2026-09-12/run_daily.txt",
        }
    )


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
