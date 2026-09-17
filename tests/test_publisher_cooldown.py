import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import publisher_cooldown as cooldown

NOW = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)


def reject(path, slug="a.test", **kwargs):
    return cooldown.record(path, slug, stage="bodies", status=429,
                           reason="HTTP 429 from a.test", **kwargs)


def test_expiry_preserves_history_when_another_source_is_written(tmp_path):
    path = tmp_path / "cooldown.json"
    first = reject(path, now=NOW)
    assert cooldown.timestamp(first["until"]) == NOW + timedelta(hours=23)
    tomorrow = NOW + timedelta(days=1)
    assert cooldown.active(path, {"a.test"}, now=tomorrow) == {}
    reject(path, "b.test", now=tomorrow)
    again = reject(path, now=tomorrow)
    assert again["consecutive_days"] == 2
    assert again["since"] == first["since"]
    assert reject(path, now=tomorrow)["consecutive_days"] == 2
    assert reject(path, now=NOW + timedelta(days=2))["consecutive_days"] == 3
    assert reject(path, now=NOW + timedelta(days=4))["consecutive_days"] == 1
    reject(path, "c.test", now=NOW + timedelta(days=12))
    assert set(cooldown.read(path)) == {"c.test"}


def test_long_retry_after_is_never_shortened(tmp_path):
    path = tmp_path / "cooldown.json"
    first = reject(path, retry_after=3 * 86400, now=NOW)
    assert cooldown.timestamp(first["until"]) == NOW + timedelta(days=3)
    assert reject(path, now=NOW + timedelta(minutes=1))["until"] == first["until"]
    assert cooldown.active(path, {"a.test"}, now=NOW + timedelta(days=2))


def test_unknown_slugs_warn_without_invalidating_known_cooldowns(tmp_path, caplog):
    path = tmp_path / "cooldown.json"
    reject(path, "removed.test", now=NOW)
    reject(path, now=NOW)
    assert set(cooldown.active(path, {"a.test"}, now=NOW)) == {"a.test"}
    assert "removed.test" in caplog.text
    assert cooldown.active(path, {"a.test"}, now=NOW + timedelta(hours=23)) == {}


def test_invalid_file_is_not_silently_ignored_or_overwritten(tmp_path):
    path = tmp_path / "cooldown.json"
    path.write_text('{"a.test":', encoding="utf-8")
    with pytest.raises(ValueError):
        cooldown.active(path, {"a.test"}, now=NOW)
    with pytest.raises(ValueError):
        reject(path, now=NOW)
    assert path.read_text(encoding="utf-8") == '{"a.test":'


def test_interrupted_replace_keeps_valid_file_and_releases_lock(tmp_path, monkeypatch):
    path = tmp_path / "cooldown.json"
    reject(path, now=NOW)
    before = path.read_bytes()

    def interrupted(*args):
        raise RuntimeError("interrupted before replace")

    with monkeypatch.context() as patch:
        patch.setattr("health.common.os.replace", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            reject(path, "b.test", now=NOW)
    assert path.read_bytes() == before
    reject(path, "b.test", now=NOW)
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"a.test", "b.test"}


def test_overlapping_processes_do_not_lose_each_others_updates(tmp_path):
    path = tmp_path / "cooldown.json"
    script = """
import sys, time
from pathlib import Path
from src import publisher_cooldown as c
original = c.read
def slow_read(path):
    value = original(path)
    time.sleep(0.15)
    return value
c.read = slow_read
c.record(Path(sys.argv[1]), sys.argv[2], stage='collect', status=403, reason='HTTP 403')
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", script, str(path), f"source-{i}.test"],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE) for i in range(4)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert set(cooldown.read(path)) == {f"source-{i}.test" for i in range(4)}
