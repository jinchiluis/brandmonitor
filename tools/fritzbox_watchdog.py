#!/usr/bin/env python3
"""Power-cycles the FRITZ!Box through its Shelly plug when the internet stays down.

Vodafone Cable sometimes leaves the FRITZ!Box up on the LAN with no WAN until its power
is cycled. The plug switches the box off and back on by itself (``Switch.Set`` with
``toggle_after``), so the recovery does not depend on the connection being repaired.
Design, safety rules and the measured boot times are in fritzbox_watchdog.md.

    python tools/fritzbox_watchdog.py --check      # probe everything once, change nothing
    python tools/fritzbox_watchdog.py --dry-run    # the real loop, but it only logs
    python tools/fritzbox_watchdog.py              # the real loop (the scheduled task)

It cycles the box only when every internet target has failed for a minute, the
FRITZ!Box answers on the LAN, and ``data/run.lock`` is explicitly free. A missing,
unreadable or held lock means do nothing: a false alarm must never interrupt a
BrandMonitor run. The lock is checked again just before the plug is told to switch.

It runs on the laptop that owns ``data/run.lock``, never on the VPS. Standard library
only; it imports nothing from ``src/`` and never opens the database.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "data" / "run.lock"
STATE_PATH = ROOT / "data" / "fritzbox_watchdog_state.json"
INSTANCE_PATH = ROOT / "data" / "fritzbox_watchdog.lock"
LOG_PATH = ROOT / "data" / "log" / "fritzbox_watchdog.log"
# One row per outage, appended when the internet returns. tools/data/ is covered by
# the `data/` rule in .gitignore, so the history stays on the host that saw the outages.
HISTORY_PATH = ROOT / "tools" / "data" / "fritzbox_outages.csv"
HISTORY_COLUMNS = ("start", "end", "duration_s", "power_cycles", "first_cycle_after_s",
                   "last_cycle_to_recovery_s", "fritz_reachable", "blocked")

# Both addresses are reserved in the FRITZ!Box (Heimnetz > Netzwerk > the device's
# "Adressen im Heimnetz"), so they stay put across reboots.
FRITZ_HOST = "192.168.178.1"
FRITZ_PORT = 80
SHELLY_HOST = "192.168.178.115"

# Down means every one of these failed. Two addresses and one name: an address
# answering says routing works, the name says DNS works too, and one unreachable
# service must not cost a reboot.
INTERNET_TCP = (("1.1.1.1", 443), ("8.8.8.8", 443))
INTERNET_URL = "https://example.com"
PROBE_TIMEOUT = 5
SHELLY_TIMEOUT = 5

CHECK_EVERY = 30        # seconds between checks
CONFIRM_AFTER = 60      # a sustained outage: still down this long after it began
OFF_SECONDS = 5         # how long the plug keeps the FRITZ!Box off
# Cable sync took about 2.5 minutes to bring the internet back on 2026-09-19, so the
# first retry at ten minutes is past any normal boot. After cycle 1, then 2, then always.
RETRY_DELAYS = (600, 900)
RETRY_REPEAT = 1800
SHELLY_RETRY = 300      # after a failed call to the plug
STABLE_AFTER = 1800     # this much uninterrupted internet ends an outage episode
HEARTBEAT = 3600        # a line saying the watchdog is alive

LOGGER = logging.getLogger("fritzbox_watchdog")


class ShellyError(Exception):
    """The plug did not answer, or answered with an error."""


# --- probes -----------------------------------------------------------------------


def tcp_up(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def https_up(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:  # any failure to fetch is "not up"
        return False


def fritz_up() -> bool:
    return tcp_up(FRITZ_HOST, FRITZ_PORT, timeout=3)


def probe_internet() -> Dict[str, bool]:
    """Every target's result, probed in parallel so a dead line costs one timeout."""
    targets: Dict[str, Callable[[], bool]] = {
        f"{host}:{port}": (lambda host=host, port=port: tcp_up(host, port))
        for host, port in INTERNET_TCP
    }
    targets[INTERNET_URL] = lambda: https_up(INTERNET_URL)
    with ThreadPoolExecutor(len(targets)) as pool:
        futures = {name: pool.submit(probe) for name, probe in targets.items()}
        return {name: future.result() for name, future in futures.items()}


def lock_state(path: Path = LOCK_PATH) -> str:
    """``free``, ``held``, ``missing`` or ``unreadable`` for the run lock.

    The batch files hold the lock as an open handle on the file (``9>data\\run.lock``),
    not as content, so asking for the file with sharing denied fails exactly while a
    run holds it. The handle is closed at once; only ``free`` allows a power-cycle.
    """
    if os.name != "nt":
        return "unreadable"
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    generic_read_write, open_existing, normal = 0xC0000000, 3, 0x80
    handle = kernel32.CreateFileW(str(path), generic_read_write, 0, None, open_existing, normal, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        if error in (2, 3):     # file / path not found
            return "missing"
        if error in (32, 33):   # sharing / lock violation
            return "held"
        return "unreadable"
    kernel32.CloseHandle(handle)
    return "free"


def shelly_rpc(host: str, method: str, params: Optional[dict] = None,
               timeout: float = SHELLY_TIMEOUT) -> dict:
    payload: dict = {"id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    request = urllib.request.Request(
        f"http://{host}/rpc", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ShellyError(f"{method}: {exc}") from exc
    if not isinstance(reply, dict) or "result" not in reply:
        raise ShellyError(f"{method}: {str(reply)[:200]}")
    return reply["result"]


def shelly_status() -> dict:
    return shelly_rpc(SHELLY_HOST, "Switch.GetStatus", {"id": 0})


def shelly_power_cycle() -> dict:
    """Off now, on again after OFF_SECONDS. The plug keeps that timer itself, so it
    completes even though the FRITZ!Box takes the Wi-Fi away with it."""
    return shelly_rpc(SHELLY_HOST, "Switch.Set", {"id": 0, "on": False, "toggle_after": OFF_SECONDS})


# --- decisions --------------------------------------------------------------------


def retry_delay(cycles: int) -> float:
    """Seconds to wait after the ``cycles``-th power-cycle before trying another."""
    if cycles <= 0:
        return 0
    return RETRY_DELAYS[cycles - 1] if cycles <= len(RETRY_DELAYS) else RETRY_REPEAT


def _stamp(epoch: float) -> str:
    """Local time with its UTC offset, as tools/data/ip-history.csv writes it."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(sep=" ", timespec="seconds")


def fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


class Watchdog:
    """One ``tick`` per check. Only the throttle (cycles, last_cycle) is persisted: a
    restart mid-outage re-confirms for a minute, but can never cycle the box early.
    That outage's history row then starts when this process first saw it fail."""

    def __init__(
        self, *, state_path: Optional[Path] = None, history_path: Optional[Path] = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time, log: Callable[[str], None] = LOGGER.info,
        probe: Callable[[], Dict[str, bool]] = probe_internet,
        fritz: Callable[[], bool] = fritz_up,
        lock: Callable[[], str] = lock_state,
        status: Callable[[], dict] = shelly_status,
        cycle: Callable[[], dict] = shelly_power_cycle,
    ) -> None:
        self.state_path = None if dry_run else state_path
        self.history_path = None if dry_run else history_path
        self.dry_run = dry_run
        self.clock, self.log = clock, log
        self._probe, self._fritz, self._lock, self._status, self._cycle = probe, fritz, lock, status, cycle
        self.cycles = 0
        self.last_cycle: Optional[float] = None
        self._load()
        self.down_since: Optional[float] = None
        self.ok_since: Optional[float] = None
        self.cycle_times: List[float] = []      # this outage's power-cycles
        self.fritz_seen: Set[bool] = set()      # what the LAN check said during it
        self.blocked: Set[str] = set()          # why a due cycle did not happen
        self.retry_at = 0.0
        self._confirmed = False
        self._noted: Optional[str] = None
        self._beat = self.clock()

    # state ------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cycles = int(data["cycles"])
            self.last_cycle = None if data["last_cycle"] is None else float(data["last_cycle"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.cycles, self.last_cycle = 0, None
            self.log(f"State file unreadable, starting fresh: {exc}")

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"cycles": self.cycles, "last_cycle": self.last_cycle}), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log(f"Could not save state: {exc}")

    def note(self, key: str, message: str) -> None:
        """Log a reason once until the reason changes, so a 30-second loop cannot spam."""
        if key not in ("wait", "fritz"):    # a delay is not a block; the LAN check has a column
            self.blocked.add(key)
        if key != self._noted:
            self._noted = key
            self.log(message)

    # loop -------------------------------------------------------------------------

    def tick(self) -> None:
        results = self._probe()
        if any(results.values()):
            self._on_up()
        else:
            self._on_down(", ".join(f"{name} down" for name in results))

    def _on_up(self) -> None:
        now = self.clock()
        if self.down_since is not None:
            self.log(f"Internet reachable again. Total outage: {fmt_duration(now - self.down_since)}, "
                     f"power cycles: {len(self.cycle_times)}")
            self._record_outage(self.down_since, now)
            self.down_since, self._confirmed, self._noted = None, False, None
        if self.ok_since is None:
            self.ok_since = now
        if self.cycles and now - self.ok_since >= STABLE_AFTER:
            self.log(f"Internet stable for {fmt_duration(STABLE_AFTER)}; power-cycle counter reset")
            self.cycles, self.last_cycle = 0, None
            self._save()
        if now - self._beat >= HEARTBEAT:
            self._beat = now
            self.log("Watching: internet reachable")

    def _on_down(self, detail: str) -> None:
        now = self.clock()
        self.ok_since = None
        if self.down_since is None:
            self.down_since, self._confirmed = now, False
            self.cycle_times, self.fritz_seen, self.blocked = [], set(), set()
            self.log(f"Internet check failed ({detail})")
            return
        if now - self.down_since < CONFIRM_AFTER:
            return
        if not self._confirmed:
            self._confirmed = True
            self.log(f"Internet still unavailable after {fmt_duration(now - self.down_since)}")
        reachable = self._fritz()
        self.fritz_seen.add(reachable)
        if not reachable:
            self.note("fritz", "FRITZ!Box not reachable locally - not power-cycling "
                               "(the box is off or hung, or this host lost its own network)")
            return
        due = max(self.retry_at, (self.last_cycle or 0) + retry_delay(self.cycles))
        if now < due:
            self.note("wait", f"Power cycle #{self.cycles + 1} not due for {fmt_duration(due - now)}")
            return
        if not self._lock_free("before checking the plug"):
            return
        try:
            output = self._status().get("output")
        except ShellyError as exc:
            self.retry_at = now + SHELLY_RETRY
            self.note("shelly", f"Shelly unreachable, cannot power-cycle: {exc}")
            return
        if not output:
            self.note("shelly-off", "Shelly output is already off - leaving it alone")
            return
        if not self._lock_free("before power-cycling"):
            return
        self._power_cycle(now)

    def _lock_free(self, when: str) -> bool:
        state = self._lock()
        if state == "free":
            return True
        self.note(f"lock-{state}", f"run.lock is {state} {when} - not power-cycling "
                                   "(only an explicitly free lock allows it)")
        return False

    def _power_cycle(self, now: float) -> None:
        number = self.cycles + 1
        self.log(f"FRITZ!Box reachable locally, internet down. Starting power cycle #{number}")
        self._noted = None
        if self.dry_run:
            self.log(f"DRY RUN: would switch the plug off for {OFF_SECONDS}s")
        else:
            try:
                self._cycle()
            except ShellyError as exc:
                self.retry_at = now + SHELLY_RETRY
                self.log(f"Power cycle #{number} FAILED, not counted: {exc}")
                return
            self.log(f"Shelly accepted power cycle #{number}: off {OFF_SECONDS}s, then on by itself")
        self.cycles, self.last_cycle = number, now
        self.cycle_times.append(now)
        self._save()

    def _record_outage(self, start: float, end: float) -> None:
        """Append this outage to the history CSV. A failure here only logs."""
        if not self.history_path:
            return
        fritz = {frozenset(): "unchecked", frozenset({True}): "yes",
                 frozenset({False}): "no"}.get(frozenset(self.fritz_seen), "mixed")
        row = {
            "start": _stamp(start), "end": _stamp(end), "duration_s": int(end - start),
            "power_cycles": len(self.cycle_times),
            "first_cycle_after_s": int(self.cycle_times[0] - start) if self.cycle_times else "",
            "last_cycle_to_recovery_s": int(end - self.cycle_times[-1]) if self.cycle_times else "",
            "fritz_reachable": fritz, "blocked": ";".join(sorted(self.blocked)),
        }
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.history_path.exists() or self.history_path.stat().st_size == 0
            with open(self.history_path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
                if new:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            self.log(f"Could not write outage history: {exc}")

    def run(self, once: bool = False) -> None:
        self.log(f"Watchdog started{' (dry run)' if self.dry_run else ''}: cycles={self.cycles}")
        while True:
            try:
                self.tick()
            except Exception:  # a probe bug must not end the loop that guards the internet
                LOGGER.exception("Unexpected error in check")
            if once:
                return
            time.sleep(CHECK_EVERY)


# --- entry point ------------------------------------------------------------------


def check() -> int:
    """Every probe once, printed. Changes nothing, so it is safe at any moment."""
    for name, up in probe_internet().items():
        print(f"internet  {name:<24} {'ok' if up else 'DOWN'}")
    print(f"fritz     {f'{FRITZ_HOST}:{FRITZ_PORT}':<24} {'reachable' if fritz_up() else 'UNREACHABLE'}")
    print(f"run.lock  {LOCK_PATH}: {lock_state()}")
    try:
        status = shelly_status()
        print(f"shelly    {SHELLY_HOST:<24} reachable, output {'on' if status.get('output') else 'OFF'}, "
              f"{status.get('apower', '?')} W")
    except ShellyError as exc:
        print(f"shelly    {SHELLY_HOST:<24} UNREACHABLE ({exc})")
    state = Watchdog(state_path=STATE_PATH)
    when = "never" if state.last_cycle is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_cycle))
    print(f"state     cycles={state.cycles} last_cycle={when}")
    return 0


def single_instance():
    """Two loops would each cycle the box, so a second real loop refuses to start."""
    import msvcrt

    INSTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(INSTANCE_PATH, "a+")
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit("another fritzbox_watchdog is already running")
    return handle  # held for the life of the process


def setup_logging() -> None:
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    if sys.stdout and sys.stdout.isatty():  # a console-less scheduled task has none
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        LOGGER.addHandler(console)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Power-cycle the FRITZ!Box via its Shelly plug when the internet stays down.")
    parser.add_argument("--check", action="store_true", help="probe everything once and print; change nothing")
    parser.add_argument("--dry-run", action="store_true", help="run the loop but only log the power cycle; no state is kept")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    setup_logging()
    instance = None if args.dry_run else single_instance()  # noqa: F841 - kept alive on purpose
    try:
        Watchdog(state_path=STATE_PATH, history_path=HISTORY_PATH, dry_run=args.dry_run).run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.info("Watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
