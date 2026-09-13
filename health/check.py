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
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_SSH_HOST = "100.80.13.120"
DEFAULT_SSH_USER = "dell laptop"
DEFAULT_MARKER_PATH = r"C:\apps\brandmonitor\data\last_run.json"
DEFAULT_QUALITY_PATH = r"C:\apps\brandmonitor\data\health\latest.json"
DEFAULT_ENV_FILE = Path("/root/cost_dashboard/.env")
DEFAULT_PUSH_ENV_FILE = Path("/etc/brandmonitor-health.env")
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_STATE_FILE = Path("/var/lib/brandmonitor-health/state.json")
DEFAULT_SENDER = "jinchilu@googlemail.com"
DEFAULT_RECIPIENT = "jinchilu@hotmail.com"
UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


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
    cycle_date: str | None = None

    def for_state(self) -> dict[str, Any]:
        return {
            "finished_utc": format_utc(self.finished_utc),
            "worst_exit": self.worst_exit,
            "stages": self.stages,
            "log": self.log,
            "cycle_date": self.cycle_date,
        }


@dataclass(frozen=True)
class Probe:
    marker: Marker | None
    error: str | None = None
    invalid_marker: bool = False


@dataclass(frozen=True)
class QualitySnapshot:
    generated_utc: datetime
    cycle_date: str
    status: str
    incidents: tuple[dict[str, str], ...]
    incident_key: str | None

    def for_state(self) -> dict[str, Any]:
        return {
            "generated_utc": format_utc(self.generated_utc),
            "cycle_date": self.cycle_date,
            "status": self.status,
            "incidents": list(self.incidents),
            "incident_key": self.incident_key,
        }


@dataclass(frozen=True)
class QualityProbe:
    snapshot: QualitySnapshot | None
    error: str | None = None
    invalid_snapshot: bool = False


@dataclass(frozen=True)
class HealthStatus:
    kind: str
    title: str
    details: tuple[str, ...]
    alert: bool
    incident_key: str | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_local(value: datetime) -> str:
    """Human-facing rendering in Europe/Berlin, for email and console text only.

    Stored/state timestamps stay on format_utc — this is presentation-only so
    the state file's parse_utc round-trip contract is untouched.
    """
    return value.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M %Z")


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
    cycle_date = raw.get("cycle_date")
    if cycle_date is not None:
        if not isinstance(cycle_date, str):
            raise MarkerError("cycle_date must be a YYYY-MM-DD string")
        try:
            datetime.strptime(cycle_date, "%Y-%m-%d")
        except ValueError as exc:
            raise MarkerError("cycle_date must be a YYYY-MM-DD string") from exc
    return Marker(
        finished_utc=parse_utc(raw.get("finished_utc"), field="finished_utc"),
        worst_exit=worst,
        stages=stages,
        log=log,
        cycle_date=cycle_date,
    )


def parse_quality_snapshot(payload: str) -> QualitySnapshot:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MarkerError(f"quality snapshot is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MarkerError("quality snapshot must be a schema_version 1 object")
    if raw.get("kind") != "coverage_health":
        raise MarkerError("quality snapshot kind must be coverage_health")
    cycle_date = raw.get("cycle_date")
    if not isinstance(cycle_date, str):
        raise MarkerError("quality cycle_date must be a YYYY-MM-DD string")
    try:
        datetime.strptime(cycle_date, "%Y-%m-%d")
    except ValueError as exc:
        raise MarkerError("quality cycle_date must be a YYYY-MM-DD string") from exc
    status = raw.get("status")
    if status not in {"learning", "healthy", "warning", "critical"}:
        raise MarkerError("quality status must be learning, healthy, warning, or critical")
    incidents_raw = raw.get("incidents")
    if not isinstance(incidents_raw, list):
        raise MarkerError("quality incidents must be a list")
    incidents: list[dict[str, str]] = []
    for item in incidents_raw:
        if not isinstance(item, dict):
            raise MarkerError("every quality incident must be an object")
        source, check, severity, message = (
            item.get("source"), item.get("check"), item.get("severity"), item.get("message")
        )
        if not all(isinstance(value, str) and value for value in (source, check, message)):
            raise MarkerError("quality incidents need source, check, and message strings")
        if severity not in {"warning", "critical"}:
            raise MarkerError("quality incident severity must be warning or critical")
        incidents.append({
            "source": source,
            "check": check,
            "severity": severity,
            "message": message,
        })
    if status in {"warning", "critical"} and not incidents:
        raise MarkerError(f"quality status {status} needs at least one incident")
    if status in {"healthy", "learning"} and incidents:
        raise MarkerError(f"quality status {status} cannot carry incidents")
    incident_key = raw.get("incident_key")
    if incident_key is not None and (
        not isinstance(incident_key, str) or not incident_key.strip()
    ):
        raise MarkerError("quality incident_key must be a non-empty string or null")
    return QualitySnapshot(
        generated_utc=parse_utc(raw.get("generated_utc"), field="quality generated_utc"),
        cycle_date=cycle_date,
        status=status,
        incidents=tuple(incidents),
        incident_key=incident_key,
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


def fetch_quality_over_ssh(
    *, host: str, user: str, quality_path: str, timeout_seconds: int
) -> QualityProbe:
    escaped_path = quality_path.replace("'", "''")
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
            return QualityProbe(None, "the ssh executable is not installed")
        except subprocess.TimeoutExpired:
            return QualityProbe(
                None, f"SSH did not finish within {timeout_seconds + 8} seconds"
            )
        if completed.returncode != 0:
            message = completed.stderr.strip().splitlines()
            reason = message[-1] if message else f"ssh exited {completed.returncode}"
            return QualityProbe(None, reason[:500])
        try:
            return QualityProbe(parse_quality_snapshot(completed.stdout))
        except MarkerError as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            return QualityProbe(None, str(exc), invalid_snapshot=True)
    raise AssertionError("unreachable")


def fetch_local_marker(path: Path) -> Probe:
    try:
        return Probe(parse_marker(path.read_text(encoding="utf-8")))
    except OSError as exc:
        return Probe(None, f"could not read {path}: {exc}")
    except MarkerError as exc:
        return Probe(None, str(exc), invalid_marker=True)


def fetch_local_quality(path: Path) -> QualityProbe:
    try:
        return QualityProbe(parse_quality_snapshot(path.read_text(encoding="utf-8")))
    except OSError as exc:
        return QualityProbe(None, f"could not read {path}: {exc}")
    except MarkerError as exc:
        return QualityProbe(None, str(exc), invalid_snapshot=True)


def evaluate(
    probe: Probe,
    *,
    checked_at: datetime,
    stale_after: timedelta,
    cached_finished_utc: str | None = None,
    quality_probe: QualityProbe | None = None,
    cached_quality_generated_utc: str | None = None,
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
            (f"finished_utc={format_local(marker.finished_utc)}",),
            True,
        )
    if age > stale_after:
        return HealthStatus(
            "stale",
            "Daily run marker is stale",
            (
                f"Last completed run: {format_local(marker.finished_utc)} ({human_age(age)} ago)",
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
                f"Completed: {format_local(marker.finished_utc)}",
                f"Non-zero stages: {failed}",
                f"Laptop log: {marker.log or '(not recorded)'}",
            ),
            True,
        )
    if quality_probe is not None:
        return evaluate_quality(
            quality_probe,
            marker=marker,
            checked_at=checked_at,
            stale_after=stale_after,
            cached_generated_utc=cached_quality_generated_utc,
        )
    return HealthStatus(
        "healthy",
        "Daily pipeline is healthy",
        (
            f"Last completed run: {format_local(marker.finished_utc)} ({human_age(age)} ago)",
            "All recorded stages exited 0.",
        ),
        False,
    )


def evaluate_quality(
    probe: QualityProbe,
    *,
    marker: Marker,
    checked_at: datetime,
    stale_after: timedelta,
    cached_generated_utc: str | None = None,
) -> HealthStatus:
    snapshot = probe.snapshot
    if snapshot is None:
        if probe.invalid_snapshot:
            return HealthStatus(
                "invalid_quality_snapshot",
                "Coverage-health snapshot is malformed",
                (probe.error or "snapshot did not satisfy its JSON contract",),
                True,
            )
        cached: datetime | None = None
        if cached_generated_utc:
            try:
                cached = parse_utc(
                    cached_generated_utc, field="cached quality generated_utc"
                )
            except MarkerError:
                cached = None
        if cached is not None and checked_at - cached <= stale_after:
            return HealthStatus(
                "quality_unreachable_grace",
                "Coverage-health snapshot temporarily unreachable",
                (
                    probe.error or "unknown probe error",
                    f"Last observed quality snapshot is only "
                    f"{human_age(checked_at - cached)} old; no alert yet.",
                ),
                False,
            )
        return HealthStatus(
            "quality_unreachable",
            "Coverage-health snapshot is unreachable",
            (probe.error or "unknown probe error",),
            True,
        )

    age = checked_at - snapshot.generated_utc
    if age < timedelta(minutes=-5):
        return HealthStatus(
            "invalid_quality_snapshot",
            "Coverage-health timestamp is in the future",
            (f"generated_utc={format_local(snapshot.generated_utc)}",),
            True,
        )
    if age > stale_after:
        return HealthStatus(
            "quality_stale",
            "Coverage-health snapshot is stale",
            (
                f"Last generated: {format_local(snapshot.generated_utc)} "
                f"({human_age(age)} ago)",
            ),
            True,
        )
    if marker.cycle_date and snapshot.cycle_date != marker.cycle_date:
        # analyze.py publishes before run_daily.bat replaces the marker. A VPS
        # read in those few seconds may see today's quality with yesterday's
        # marker; that direction is a harmless in-progress transition.
        if snapshot.cycle_date > marker.cycle_date:
            return HealthStatus(
                "quality_transition",
                "Daily health files are being updated",
                (
                    f"Run marker cycle {marker.cycle_date}; coverage cycle "
                    f"{snapshot.cycle_date}. No alert during forward transition.",
                ),
                False,
            )
        return HealthStatus(
            "quality_cycle_mismatch",
            "Coverage-health snapshot does not match the daily run",
            (
                f"Run marker cycle {marker.cycle_date}; coverage cycle "
                f"{snapshot.cycle_date}.",
            ),
            True,
        )
    if snapshot.status in {"warning", "critical"}:
        details = [
            f"Coverage analysis generated {format_local(snapshot.generated_utc)}."
        ]
        for incident in snapshot.incidents[:8]:
            details.append(
                f"{incident['severity']} {incident['source']} "
                f"[{incident['check']}]: {incident['message']}"
            )
        if len(snapshot.incidents) > 8:
            details.append(f"...and {len(snapshot.incidents) - 8} more incident(s).")
        return HealthStatus(
            f"quality_{snapshot.status}",
            f"Coverage health is {snapshot.status}",
            tuple(details),
            True,
            snapshot.incident_key or f"quality_{snapshot.status}",
        )
    if snapshot.status == "learning":
        return HealthStatus(
            "healthy",
            "Daily pipeline is healthy; coverage baselines are learning",
            (
                f"Last completed run: {format_local(marker.finished_utc)}.",
                f"Coverage snapshot: {format_local(snapshot.generated_utc)}.",
            ),
            False,
        )
    return HealthStatus(
        "healthy",
        "Daily pipeline and coverage are healthy",
        (
            f"Last completed run: {format_local(marker.finished_utc)}.",
            f"Coverage snapshot: {format_local(snapshot.generated_utc)}.",
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
    notified_key = state.get("notified_key") or notified_kind
    if status.alert:
        current_key = status.incident_key or status.kind
        return "alert" if notified_key != current_key else None
    if status.kind == "healthy" and notified_kind:
        return "recovery"
    return None


def read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HealthCheckError(f"cannot read env file {path}: {exc}") from exc
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


@dataclass(frozen=True)
class PushSettings:
    server: str
    topic: str
    token: str | None = None


def push_settings(path: Path) -> PushSettings | None:
    """ntfy settings, or None when push is not configured on this host.

    The topic name is the credential on a public server, so it lives in a
    root-only file outside the repository rather than in the unit file.
    """
    values = read_env_file(path) if path.exists() else {}
    topic = (os.environ.get("NTFY_TOPIC") or values.get("NTFY_TOPIC", "")).strip()
    if not topic:
        return None
    server = os.environ.get("NTFY_SERVER") or values.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER
    token = os.environ.get("NTFY_TOKEN") or values.get("NTFY_TOKEN") or None
    return PushSettings(server.strip().rstrip("/"), topic, token)


def push_priority(action: str, status: HealthStatus) -> int:
    """ntfy priority: 5 urgent, 4 high, 3 default, 2 low."""
    if action == "recovery":
        return 2
    if status.kind == "quality_warning":
        return 3
    if status.kind.startswith("quality_"):
        return 4
    return 5


def send_push(
    settings: PushSettings, *, title: str, message: str, priority: int, tags: list[str]
) -> bool:
    body = json.dumps({
        "topic": settings.topic,
        "title": title,
        "message": message[:3500],
        "priority": priority,
        "tags": tags,
    }).encode("utf-8")
    request = urllib.request.Request(
        settings.server, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if settings.token:
        request.add_header("Authorization", f"Bearer {settings.token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except (OSError, urllib.error.URLError) as exc:
        print(f"ERROR: failed to send push: {exc}", file=sys.stderr)
        return False
    print(f"Sent push to {settings.server}: {title}")
    return True


def deliver(
    args: argparse.Namespace, *, subject: str, paragraphs: list[str],
    priority: int, tags: list[str],
) -> bool:
    """Send on every configured channel; one successful channel latches the incident.

    Requiring both would turn a Gmail outage into a push every fifteen minutes,
    because an unlatched incident is retried on each timer run.
    """
    push = push_settings(args.push_env_file)
    delivered = False
    try:
        sender, recipient, password = smtp_settings(args)
    except HealthCheckError as exc:
        if push is None:
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
    else:
        delivered = send_email(
            sender=sender, recipient=recipient, password=password,
            subject=subject, paragraphs=paragraphs,
        )
    if push is not None:
        pushed = send_push(
            push, title=subject, message="\n\n".join(paragraphs),
            priority=priority, tags=tags,
        )
        delivered = delivered or pushed
    return delivered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--marker-path", default=DEFAULT_MARKER_PATH)
    parser.add_argument("--quality-path", default=DEFAULT_QUALITY_PATH)
    parser.add_argument(
        "--marker-file", type=Path,
        help="read a local marker instead of SSH (for development/testing)",
    )
    parser.add_argument(
        "--quality-file", type=Path,
        help="read a local coverage snapshot instead of SSH (with --marker-file)",
    )
    parser.add_argument("--ssh-timeout", type=int, default=10)
    parser.add_argument("--stale-hours", type=float, default=26.0)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--push-env-file", type=Path, default=DEFAULT_PUSH_ENV_FILE,
        help="file with NTFY_TOPIC (and optional NTFY_SERVER, NTFY_TOKEN); "
             "push is off when it is absent",
    )
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
    parser.add_argument(
        "--test-push", action="store_true",
        help="send one ntfy test push without probing or changing state",
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
                f"Sent: {format_local(now_utc())}",
            ],
        )
        return 0 if ok else 2

    if args.test_push:
        push = push_settings(args.push_env_file)
        if push is None:
            raise HealthCheckError(f"NTFY_TOPIC is not set in {args.push_env_file}")
        ok = send_push(
            push,
            title="[brandmonitor] health push test",
            message=f"The VPS health checker on {socket.gethostname()} can push.\n"
                    f"Sent: {format_local(now_utc())}",
            priority=3,
            tags=["white_check_mark"],
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
    if args.marker_file:
        quality_path = args.quality_file or args.marker_file.parent / "health" / "latest.json"
        quality_probe = fetch_local_quality(quality_path)
    else:
        quality_probe = fetch_quality_over_ssh(
            host=args.ssh_host,
            user=args.ssh_user,
            quality_path=args.quality_path,
            timeout_seconds=args.ssh_timeout,
        )
    cached_marker = state.get("last_marker")
    cached_finished = (
        cached_marker.get("finished_utc") if isinstance(cached_marker, dict) else None
    )
    cached_quality = state.get("last_quality")
    cached_quality_generated = (
        cached_quality.get("generated_utc") if isinstance(cached_quality, dict) else None
    )
    status = evaluate(
        probe,
        checked_at=checked_at,
        stale_after=timedelta(hours=args.stale_hours),
        cached_finished_utc=cached_finished,
        quality_probe=quality_probe,
        cached_quality_generated_utc=cached_quality_generated,
    )
    print(f"{format_utc(checked_at)} {status.kind}: {status.title}")
    for detail in status.details:
        print(f"  {detail}")

    action = notification_action(status, state)
    if args.dry_run:
        print(f"Dry run: notification={action or 'none'}; state unchanged")
        return 0

    prior_incident_key = state.get("notified_key") or state.get("notified_kind")
    prior_details = list(state.get("details") or [])

    sent = False
    if action:
        if action == "alert":
            subject = f"[brandmonitor] ALERT: {status.title}"
            paragraphs = ["Brand Monitor needs attention.", *status.details]
        else:
            subject = "[brandmonitor] RECOVERED: daily pipeline is healthy"
            paragraphs = ["Brand Monitor has recovered from the previous health incident."]
            if prior_incident_key:
                paragraphs.append(f"Resolved incident: {prior_incident_key}")
            paragraphs.extend(prior_details)
            paragraphs.extend(status.details)
        sent = deliver(
            args,
            subject=subject,
            paragraphs=[
                *paragraphs,
                f"Checked by VPS {socket.gethostname()} at {format_local(checked_at)}.",
            ],
            priority=push_priority(action, status),
            tags=["rotating_light"] if action == "alert" else ["white_check_mark"],
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
    if quality_probe.snapshot is not None:
        state["last_quality"] = quality_probe.snapshot.for_state()
        state["last_quality_reachable_utc"] = format_utc(checked_at)
    if action == "alert" and sent:
        state["notified_kind"] = status.kind
        state["notified_key"] = status.incident_key or status.kind
        state["alert_sent_utc"] = format_utc(checked_at)
    elif action == "recovery" and sent:
        state.pop("notified_kind", None)
        state.pop("notified_key", None)
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
