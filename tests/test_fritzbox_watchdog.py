"""The FRITZ!Box watchdog: when it may power-cycle the router, and when it must not."""

import csv
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.fritzbox_watchdog as fw  # noqa: E402

DOWN = {"1.1.1.1:443": False, "8.8.8.8:443": False, "https://example.com": False}
UP = {"1.1.1.1:443": True, "8.8.8.8:443": True, "https://example.com": True}


class Rig:
    """A watchdog wired to fakes; ``advance`` moves the clock and ticks every 30 s."""

    def __init__(self, tmp_path=None, dry_run=False, history=False):
        self.now = 1_000_000.0
        self.internet = dict(DOWN)
        self.fritz = True
        self.lock = "free"
        self.lock_seq = None            # states handed out in order, then ``lock``
        self.output = True
        self.shelly_error = None
        self.cycled = []
        self.lines = []
        self.state_path = tmp_path / "state.json" if tmp_path else None
        self.history_path = tmp_path / "tools" / "data" / "outages.csv" if tmp_path and history else None
        self.dry_run = dry_run
        self.dog = self.build()

    def build(self):
        return fw.Watchdog(
            state_path=self.state_path, history_path=self.history_path, dry_run=self.dry_run,
            clock=lambda: self.now,
            log=self.lines.append, probe=lambda: dict(self.internet), fritz=lambda: self.fritz,
            lock=self._lock, status=self._status, cycle=self._cycle,
        )

    def _lock(self):
        if self.lock_seq:
            return self.lock_seq.pop(0)
        return self.lock

    def _status(self):
        if self.shelly_error:
            raise fw.ShellyError(self.shelly_error)
        return {"output": self.output, "apower": 9.5}

    def _cycle(self):
        if self.shelly_error:
            raise fw.ShellyError(self.shelly_error)
        self.cycled.append(self.now)
        return {"was_on": True}

    def advance(self, seconds, step=30):
        end = self.now + seconds
        while self.now < end:
            self.now += step
            self.dog.tick()

    def text(self):
        return "\n".join(self.lines)

    def rows(self):
        if not self.history_path.exists():
            return []
        with open(self.history_path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))


def test_healthy_internet_does_nothing():
    rig = Rig()
    rig.internet = dict(UP)
    rig.advance(600)
    assert rig.cycled == [] and rig.lines == []


def test_one_answering_target_means_the_internet_is_up():
    rig = Rig()
    rig.internet = {**DOWN, "8.8.8.8:443": True}
    rig.advance(600)
    assert rig.cycled == []


def test_a_short_blip_is_not_cycled_and_is_logged_as_an_outage():
    rig = Rig()
    rig.advance(30)                       # first failure
    rig.internet = dict(UP)
    rig.advance(30)
    assert rig.cycled == []
    assert "Internet check failed" in rig.text()
    assert "Total outage: 0m 30s" in rig.text()


def test_sustained_outage_cycles_once_after_the_confirmation_minute():
    rig = Rig()
    rig.advance(30)                       # failure begins
    rig.advance(30)                       # 30 s in: not yet
    assert rig.cycled == []
    rig.advance(30)                       # 60 s in: confirmed
    assert len(rig.cycled) == 1
    assert "Starting power cycle #1" in rig.text()
    rig.advance(300)
    assert len(rig.cycled) == 1           # the retry delay holds


def test_recovery_after_a_cycle_reports_outage_and_cycle_count():
    rig = Rig()
    rig.advance(90)                       # failure at 30 s, cycled at 90 s
    rig.internet = dict(UP)
    rig.advance(30)
    assert "Total outage: 1m 30s, power cycles: 1" in rig.text()


def test_retry_schedule_is_10_then_15_then_30_minutes():
    rig = Rig()
    rig.advance(90)
    first = rig.cycled[0]
    rig.advance(4 * 3600)
    gaps = [b - a for a, b in zip(rig.cycled, rig.cycled[1:])]
    assert rig.cycled[0] == first
    assert gaps[:4] == [600, 900, 1800, 1800]


def test_retry_delay_values():
    assert [fw.retry_delay(n) for n in (0, 1, 2, 3, 4, 9)] == [0, 600, 900, 1800, 1800, 1800]


def test_a_reachable_fritzbox_is_required():
    rig = Rig()
    rig.fritz = False
    rig.advance(600)
    assert rig.cycled == []
    assert "not reachable locally" in rig.text()


@pytest.mark.parametrize("state", ["held", "missing", "unreadable"])
def test_only_an_explicitly_free_lock_allows_a_cycle(state):
    rig = Rig()
    rig.lock = state
    rig.advance(900)
    assert rig.cycled == []
    assert f"run.lock is {state}" in rig.text()
    rig.lock = "free"
    rig.advance(30)
    assert len(rig.cycled) == 1           # and the moment it frees, recovery runs


def test_a_run_starting_during_the_checks_stops_the_cycle():
    rig = Rig()
    rig.advance(30)
    rig.lock_seq = ["free", "held"]       # free at the first look, held just before the plug
    rig.advance(60)
    assert rig.cycled == []
    assert "run.lock is held before power-cycling" in rig.text()


def test_the_lock_is_checked_twice_around_the_plug_check():
    rig = Rig()
    asked = []
    rig.dog._lock = lambda: asked.append(1) or "free"
    rig.advance(90)
    assert len(asked) == 2 and len(rig.cycled) == 1


def test_a_repeated_reason_is_logged_once():
    rig = Rig()
    rig.lock = "held"
    rig.advance(1800)
    assert rig.text().count("run.lock is held") == 1


def test_unreachable_plug_is_not_counted_and_retried_after_five_minutes():
    rig = Rig()
    rig.shelly_error = "timed out"
    rig.advance(120)
    assert rig.cycled == [] and rig.dog.cycles == 0
    assert "Shelly unreachable" in rig.text()
    rig.shelly_error = None
    rig.advance(120)
    assert rig.cycled == []               # still inside the 300 s back-off
    rig.advance(300)
    assert len(rig.cycled) == 1


def test_a_failing_switch_call_is_not_counted():
    rig = Rig()
    rig._status = lambda: {"output": True}
    rig.dog._status = rig._status
    rig.dog._cycle = lambda: (_ for _ in ()).throw(fw.ShellyError("HTTP 500"))
    rig.advance(90)
    assert rig.dog.cycles == 0
    assert "FAILED, not counted" in rig.text()


def test_a_plug_already_off_is_left_alone():
    rig = Rig()
    rig.output = False
    rig.advance(600)
    assert rig.cycled == []


def test_throttle_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.advance(90)
    assert len(rig.cycled) == 1
    rig.dog = rig.build()                 # the process restarted mid-outage
    assert rig.dog.cycles == 1
    rig.advance(300)                      # re-confirms, but the 600 s delay still holds
    assert len(rig.cycled) == 1
    rig.advance(400)
    assert len(rig.cycled) == 2


def test_counter_resets_only_after_stable_internet():
    rig = Rig()
    rig.advance(90)
    rig.internet = dict(UP)
    rig.advance(fw.STABLE_AFTER + 30)
    assert rig.dog.cycles == 0
    rig.internet = dict(DOWN)
    rig.advance(90)
    assert len(rig.cycled) == 2           # a fresh episode cycles after one minute again


def test_a_relapse_soon_after_a_cycle_keeps_the_throttle():
    rig = Rig()
    rig.advance(90)
    rig.internet = dict(UP)
    rig.advance(60)                       # up for a minute: not stable yet
    rig.internet = dict(DOWN)
    rig.advance(120)
    assert len(rig.cycled) == 1           # the ten-minute delay still applies


def test_dry_run_never_touches_the_plug_or_the_state_file(tmp_path):
    rig = Rig(tmp_path, dry_run=True)
    rig.advance(120)
    assert rig.cycled == []
    assert "DRY RUN: would switch the plug off" in rig.text()
    assert not (tmp_path / "state.json").exists()


def test_a_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    rig = Rig(tmp_path)
    assert rig.dog.cycles == 0
    assert "State file unreadable" in rig.text()


def test_a_blip_is_recorded_with_no_cycles(tmp_path):
    rig = Rig(tmp_path, history=True)
    rig.advance(30)
    rig.internet = dict(UP)
    rig.advance(30)
    (row,) = rig.rows()
    assert list(row) == list(fw.HISTORY_COLUMNS)
    assert row["duration_s"] == "30" and row["power_cycles"] == "0"
    assert row["first_cycle_after_s"] == "" and row["last_cycle_to_recovery_s"] == ""
    assert row["fritz_reachable"] == "unchecked" and row["blocked"] == ""


def test_a_cycled_outage_is_recorded_with_its_timings(tmp_path):
    rig = Rig(tmp_path, history=True)
    rig.advance(90)                       # failure at 30 s, cycled at 90 s
    rig.advance(120)                      # still down
    rig.internet = dict(UP)
    rig.advance(30)                       # back at 240 s
    (row,) = rig.rows()
    assert row["duration_s"] == "210"
    assert row["power_cycles"] == "1"
    assert row["first_cycle_after_s"] == "60"
    assert row["last_cycle_to_recovery_s"] == "150"
    assert row["fritz_reachable"] == "yes"
    start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
    assert start.utcoffset() is not None          # local time with its UTC offset
    assert (end - start).total_seconds() == 210


def test_history_says_what_blocked_a_cycle_and_when_the_lan_was_down(tmp_path):
    rig = Rig(tmp_path, history=True)
    rig.lock = "held"
    rig.advance(300)
    rig.lock = "free"
    rig.fritz = False
    rig.advance(120)
    rig.internet = dict(UP)
    rig.advance(30)
    (row,) = rig.rows()
    assert row["blocked"] == "lock-held"
    assert row["fritz_reachable"] == "mixed"
    assert row["power_cycles"] == "0"


def test_each_outage_is_its_own_row_with_one_header(tmp_path):
    rig = Rig(tmp_path, history=True)
    for _ in range(3):
        rig.internet = dict(DOWN)
        rig.advance(90)
        rig.internet = dict(UP)
        rig.advance(30)
    assert [row["power_cycles"] for row in rig.rows()] == ["1", "0", "0"]
    assert rig.history_path.read_text(encoding="utf-8").count("start,end") == 1


def test_an_unwritable_history_only_logs(tmp_path):
    rig = Rig(tmp_path, history=True)
    rig.history_path.mkdir(parents=True)  # a directory where the file should be
    rig.advance(30)
    rig.internet = dict(UP)
    rig.advance(30)
    assert "Could not write outage history" in rig.text()


def test_dry_run_writes_no_history(tmp_path):
    rig = Rig(tmp_path, dry_run=True, history=True)
    rig.advance(90)
    rig.internet = dict(UP)
    rig.advance(30)
    assert not rig.history_path.exists()


def test_durations():
    assert fw.fmt_duration(366) == "6m 06s"
    assert fw.fmt_duration(3725) == "1h 02m 05s"


# --- the real lock probe and the real plug request --------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the run lock is a Windows handle")
def test_lock_probe_sees_free_held_and_missing(tmp_path):
    path = tmp_path / "run.lock"
    assert fw.lock_state(path) == "missing"
    path.write_bytes(b"")
    assert fw.lock_state(path) == "free"
    with open(path, "rb"):
        assert fw.lock_state(path) == "held"
    assert fw.lock_state(path) == "free"  # and the probe let go of it


@pytest.mark.skipif(os.name != "nt", reason="the run lock is a Windows handle")
def test_lock_probe_sees_the_batch_files_redirect_hold(tmp_path):
    """`9>file ( ... )` is exactly how run_daily.bat holds data\\run.lock."""
    import subprocess

    path = tmp_path / "run.lock"
    script = tmp_path / "hold.bat"
    script.write_text(f'@echo off\r\n9>"{path}" ( ping -n 6 127.0.0.1 >nul )\r\n', encoding="ascii")
    proc = subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            if path.exists() and fw.lock_state(path) == "held":
                break
            threading.Event().wait(0.1)
        assert fw.lock_state(path) == "held"
    finally:
        proc.wait()
    assert fw.lock_state(path) == "free"


class PlugHandler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        PlugHandler.seen.append((self.path, json.loads(body)))
        payload = json.dumps({"id": 1, "src": "plug", "result": {"was_on": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_power_cycle_request_matches_the_documented_command(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), PlugHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        PlugHandler.seen.clear()
        monkeypatch.setattr(fw, "SHELLY_HOST", f"127.0.0.1:{server.server_port}")
        assert fw.shelly_power_cycle() == {"was_on": True}
    finally:
        server.shutdown()
    assert PlugHandler.seen == [
        ("/rpc", {"id": 1, "method": "Switch.Set", "params": {"id": 0, "on": False, "toggle_after": 5}})
    ]


def test_an_unreachable_plug_raises_shelly_error():
    with pytest.raises(fw.ShellyError):
        fw.shelly_rpc("127.0.0.1:9", "Switch.GetStatus", timeout=1)
