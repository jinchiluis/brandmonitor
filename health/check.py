#!/usr/bin/env python3
"""Check the laptop's daily-run marker and email once per incident.

This script is intended to run on the VPS.  It pulls ``last_run.json`` over the
existing VPS-to-laptop SSH connection, evaluates freshness before exit codes,
and stores a small local latch so an incident produces one alert and one
recovery email rather than a message every fifteen minutes.

No project database or free-form log parsing happens here.  The laptop owns the
pipeline's health judgement; this checker consumes its structured marker.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any


DEFAULT_SSH_HOST = "100.80.13.120"
DEFAULT_SSH_USER = "dell laptop"
DEFAULT_MARKER_PATH = r"C:\apps\brandmonitor\data\last_run.json"
DEFAULT_ENV_FILE = Path("/root/cost_dashboard/.env")
DEFAULT_STATE_FILE = Path("/var/lib/brandmonitor-health/state.json")
DEFAULT_SENDER = "jinchilu@googlemail.com"
DEFAULT_RECIPIENT = "jinchilu@hotmail.com"
UTC = timezone.utc


class HealthCheckError(RuntimeError):
    """The checker itself is misconfigured or unable to persist its state."""


class MarkerError(ValueError):
    """The remote marker exists but violates its documented contract."""


@dataclass(frozen=True)
class Marker:
    finished_utc: datetime
    worst_exit: int
    stages: dict[str, int]
    log: str

    def for_state(self) -> dict[str, Any]:
        return {
            "finished_utc": format_utc(self.finished_utc),
            "worst_exit": self.worst_exit,
            "stages": self.stages,
            "log": self.log,
        }


@dataclass(frozen=True)
class Probe:
    marker: Marker | None
    error: str | None = None
    invalid_marker: bool = False


@dataclass(frozen=True)
class HealthStatus:
    kind: str
    title: str
    details: tuple[str, ...]
    alert: bool


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MarkerError(f"{field} must be a non-empty timestamp string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MarkerError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MarkerError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def parse_marker(payload: str) -> Marker:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MarkerError(f"marker is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise MarkerError("marker root must be an object")

    worst = raw.get("worst_exit")
    if isinstance(worst, bool) or not isinstance(worst, int) or worst not in range(4):
        raise MarkerError("worst_exit must be an integer from 0 through 3")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, dict) or not stages_raw:
        raise MarkerError("stages must be a non-empty object")
    stages: dict[str, int] = {}
    for name, code in stages_raw.items():
        if not isinstance(name, str) or not name:
            raise MarkerError("every stage needs a non-empty string name")
        if isinstance(code, bool) or not isinstance(code, int) or code not in range(4):
            raise MarkerError(f"stage {name!r} has an invalid exit code")
        stages[name] = code
    if max(stages.values()) != worst:
        raise MarkerError("worst_exit does not match the highest stage exit code")

    log = raw.get("log", "")
    if not isinstance(log, str):
        raise MarkerError("log must be a string")
    return Marker(
        finished_utc=parse_utc(raw.get("finished_utc"), field="finished_utc"),
        worst_exit=worst,
        stages=stages,
        log=log,
    )


def fetch_marker_over_ssh(
    *, host: str, user: str, marker_path: str, timeout_seconds: int
) -> Probe:
    escaped_path = marker_path.replace("'", "''")
    powershell = f"Get-Content -Raw -LiteralPath '{escaped_path}'"
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout_seconds}",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1",
        "-l", user,
        host,
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell,
    ]
    for attempt in range(2):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 8,
                check=False,
            )
        except FileNotFoundError:
            return Probe(None, "the ssh executable is not installed")
        except subprocess.TimeoutExpired:
            return Probe(None, f"SSH did not finish within {timeout_seconds + 8} seconds")

        if completed.returncode != 0:
            message = completed.stderr.strip().splitlines()
            reason = message[-1] if message else f"ssh exited {completed.returncode}"
            return Probe(None, reason[:500])
        try:
            return Probe(parse_marker(completed.stdout))
        except MarkerError as exc:
            if attempt == 0:
                # run_daily.bat currently replaces the marker in place. Avoid a
                # false incident if this read landed inside that very short write.
                time.sleep(1)
                continue
            return Probe(None, str(exc), invalid_marker=True)
    raise AssertionError("unreachable")


def fetch_local_marker(path: Path) -> Probe:
    try:
        return Probe(parse_marker(path.read_text(encoding="utf-8")))
    except OSError as exc:
        return Probe(None, f"could not read {path}: {exc}")
    except MarkerError as exc:
        return Probe(None, str(exc), invalid_marker=True)


def evaluate(
    probe: Probe,
    *,
    checked_at: datetime,
    stale_after: timedelta,
    cached_finished_utc: str | None = None,
) -> HealthStatus:
    if probe.marker is None:
        if probe.invalid_marker:
            return HealthStatus(
                "invalid_marker",
                "Run marker is malformed",
                (probe.error or "marker did not satisfy its JSON contract",),
                True,
            )
        cached: datetime | None = None
        if cached_finished_utc:
            try:
                cached = parse_utc(cached_finished_utc, field="cached finished_utc")
            except MarkerError:
                cached = None
        if cached is not None and checked_at - cached <= stale_after:
            age = checked_at - cached
            return HealthStatus(
                "unreachable_grace",
                "Laptop temporarily unreachable",
                (
                    probe.error or "unknown probe error",
                    f"Last observed completed run is only {human_age(age)} old; no alert yet.",
                ),
                False,
            )
        detail = probe.error or "unknown probe error"
        if cached is not None:
            detail += f"; last observed completed run is {human_age(checked_at - cached)} old"
        return HealthStatus(
            "unreachable",
            "Laptop or run marker is unreachable",
            (detail,),
            True,
        )

    marker = probe.marker
    age = checked_at - marker.finished_utc
    if age < timedelta(minutes=-5):
        return HealthStatus(
            "invalid_marker",
            "Run marker timestamp is in the future",
            (f"finished_utc={format_utc(marker.finished_utc)}",),
            True,
        )
    if age > stale_after:
        return HealthStatus(
            "stale",
            "Daily run marker is stale",
            (
                f"Last completed run: {format_utc(marker.finished_utc)} ({human_age(age)} ago)",
                f"Last recorded worst exit: {marker.worst_exit}",
                f"Laptop log: {marker.log or '(not recorded)'}",
            ),
            True,
        )
    if marker.worst_exit:
        failed = ", ".join(
            f"{name}={code}" for name, code in marker.stages.items() if code
        )
        return HealthStatus(
            "run_failed",
            f"Daily run completed with exit {marker.worst_exit}",
            (
                f"Completed: {format_utc(marker.finished_utc)}",
                f"Non-zero stages: {failed}",
                f"Laptop log: {marker.log or '(not recorded)'}",
            ),
            True,
        )
    return HealthStatus(
        "healthy",
        "Daily pipeline is healthy",
        (
            f"Last completed run: {format_utc(marker.finished_utc)} ({human_age(age)} ago)",
            "All recorded stages exited 0.",
        ),
        False,
    )


def human_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1}
    except (OSError, json.JSONDecodeError) as exc:
        raise HealthCheckError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise HealthCheckError(f"unsupported state file at {path}")
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise HealthCheckError(f"cannot write state file {path}: {exc}") from exc


def notification_action(status: HealthStatus, state: dict[str, Any]) -> str | None:
    notified_kind = state.get("notified_kind")
    if status.alert:
        return "alert" if notified_kind != status.kind else None
    if status.kind == "healthy" and notified_kind:
        return "recovery"
    return None


def read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HealthCheckError(f"cannot read SMTP env file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def smtp_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    file_values = read_env_file(args.env_file)
    sender = args.sender or os.environ.get("EMAIL_SENDER") or file_values.get(
        "EMAIL_SENDER", DEFAULT_SENDER
    )
    recipient = args.recipient or os.environ.get("EMAIL_RECIPIENT") or file_values.get(
        "EMAIL_RECIPIENT", DEFAULT_RECIPIENT
    )
    password = os.environ.get("SMTP_PASSWORD") or file_values.get("SMTP_PASSWORD", "")
    password = password.replace(" ", "")
    if not password:
        raise HealthCheckError("SMTP_PASSWORD is missing")
    return sender, recipient, password


def send_email(
    *, sender: str, recipient: str, password: str, subject: str, paragraphs: list[str]
) -> bool:
    text_body = "\n\n".join(paragraphs) + "\n"
    html_body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"Brand Monitor Health <{sender}>"
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(sender, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"ERROR: failed to send email: {exc}", file=sys.stderr)
        return False
    print(f"Sent email to {recipient}: {subject}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--marker-path", default=DEFAULT_MARKER_PATH)
    parser.add_argument(
        "--marker-file", type=Path,
        help="read a local marker instead of SSH (for development/testing)",
    )
    parser.add_argument("--ssh-timeout", type=int, default=10)
    parser.add_argument("--stale-hours", type=float, default=26.0)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--sender")
    parser.add_argument("--recipient")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and print without emailing or changing state",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help="send one SMTP test email without probing or changing state",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.ssh_timeout < 1:
        raise HealthCheckError("--ssh-timeout must be at least 1")
    if args.stale_hours <= 0:
        raise HealthCheckError("--stale-hours must be positive")

    if args.test_email:
        sender, recipient, password = smtp_settings(args)
        ok = send_email(
            sender=sender,
            recipient=recipient,
            password=password,
            subject="[brandmonitor] health email test",
            paragraphs=[
                "The Brand Monitor VPS health checker can send email.",
                f"VPS host: {socket.gethostname()}",
                f"Sent: {format_utc(now_utc())}",
            ],
        )
        return 0 if ok else 2

    state = load_state(args.state_file)
    checked_at = now_utc()
    probe = (
        fetch_local_marker(args.marker_file)
        if args.marker_file
        else fetch_marker_over_ssh(
            host=args.ssh_host,
            user=args.ssh_user,
            marker_path=args.marker_path,
            timeout_seconds=args.ssh_timeout,
        )
    )
    cached_marker = state.get("last_marker")
    cached_finished = (
        cached_marker.get("finished_utc") if isinstance(cached_marker, dict) else None
    )
    status = evaluate(
        probe,
        checked_at=checked_at,
        stale_after=timedelta(hours=args.stale_hours),
        cached_finished_utc=cached_finished,
    )
    print(f"{format_utc(checked_at)} {status.kind}: {status.title}")
    for detail in status.details:
        print(f"  {detail}")

    action = notification_action(status, state)
    if args.dry_run:
        print(f"Dry run: notification={action or 'none'}; state unchanged")
        return 0

    sent = False
    if action:
        sender, recipient, password = smtp_settings(args)
        if action == "alert":
            subject = f"[brandmonitor] ALERT: {status.title}"
            opening = "Brand Monitor needs attention."
        else:
            subject = "[brandmonitor] RECOVERED: daily pipeline is healthy"
            opening = "Brand Monitor has recovered from the previous health incident."
        sent = send_email(
            sender=sender,
            recipient=recipient,
            password=password,
            subject=subject,
            paragraphs=[
                opening,
                *status.details,
                f"Checked by VPS {socket.gethostname()} at {format_utc(checked_at)}.",
            ],
        )
        if not sent:
            return 2

    state["version"] = 1
    state["checked_utc"] = format_utc(checked_at)
    state["condition"] = status.kind
    state["details"] = list(status.details)
    if probe.marker is not None:
        state["last_marker"] = probe.marker.for_state()
        state["last_reachable_utc"] = format_utc(checked_at)
    if action == "alert" and sent:
        state["notified_kind"] = status.kind
        state["alert_sent_utc"] = format_utc(checked_at)
    elif action == "recovery" and sent:
        state.pop("notified_kind", None)
        state["recovery_sent_utc"] = format_utc(checked_at)
    save_state(args.state_file, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except HealthCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
