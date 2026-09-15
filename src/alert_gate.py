"""Daily news alert pass over items already admitted by the relevance gates.

This is deliberately small.  It does not discover news, assign urgency, or send
one message per article.  It takes the news bodies that became eligible since the
previous invocation, cheaply prefilters them with the client's own-brand and alert
vocabulary, and asks one model question: is this worth an immediate human look as
a potential alert?  All positive answers are collected into one email to the human
reviewer.

News reaches this stage through either of the two existing routes:

* a relevant or unsure news body-gate decision; or
* a successful body fetch caused by a title-gate keep (those intentionally skip
  the body gate); or
* a title-gate keep whose body fetch ended ``unavailable`` - a paywall or a
  subscriber login. The title gate already judged it, so it is offered on its
  title alone, without the term prefilter a missing body could never pass.

Decisions have their own table rather than ``assessment``.  The latter is the
weekly relevance ledger, and alerting is an additional action on top of that
ledger.  The alert watermark advances after every candidate was decided.  Email
delivery is separate: positive rows stay unsent and are retried together on the
next invocation if SMTP fails.

One item must never hold up the others. A model that twice answers in the wrong
shape gets a flagged "could not decide" alert, so a person looks - the reviewer
is a human and recall is the point. A call that fails outright leaves that item
undecided and the watermark where it is, so the next run retries it, while every
other item is still decided and every decided alert still sent. Three such
outcomes in a row stop the run: that is an outage or a broken model, not an item.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from src.config import ALERT_GATE_MODEL, ALERT_GATE_REASONING, BODY_GATE_BODY_CHARS, ROOT
from src.db import DB_PATH, advance_watermark, connect, get_watermark, session, utcnow
from src.logger import get_logger
from src.profile import ClientProfile, Rule, compile_term
from src.selector import normalized
from src.title_gate import Caller, GateConfigError, openai_caller as _openai_caller


STAGE = "alert_gate"
KIND = "news"
WATERMARK_PREFIX = "analysis"
TAXONOMY_NAME = "alert_taxonomy.json"
MAX_ATTEMPTS = 2
PUSH_CAP = 5
PUSH_TITLE_BYTES = 250
PUSH_SUMMARY_BYTES = 1500
# An alert is for something happening now; older material belongs in the weekly
# report. A new source's first crawl or a re-keyed URL brings in whole archives.
STALE_DAYS = 14
BERLIN = ZoneInfo("Europe/Berlin")
MAX_FAILURES_IN_A_ROW = 3
# The summary a flagged fail-open alert carries in place of the model's.
FAIL_OPEN_SUMMARY = "模型两次未给出可用判断，未能自动筛查；请人工查看原文。"
# Appended to the title of an alert judged without its body.
BODY_UNAVAILABLE_MARK = "(正文不可用)"
UNAVAILABLE_ROUTE = "title_gate_unavailable"

logger = get_logger(__name__)

REPLY_FORMAT = {
    "type": "json_schema",
    "name": "alert_gate",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "potential_alert": {"type": "boolean"},
            "summary_zh": {"type": "string"},
        },
        "required": ["potential_alert", "summary_zh"],
        "additionalProperties": False,
    },
}


class AlertGateError(RuntimeError):
    """The alert stage could not finish deciding every offered item."""


class AlertConfigError(AlertGateError):
    """A required taxonomy, model, or SMTP setting is absent or invalid."""


class AlertDeliveryError(AlertGateError):
    """The combined alert email could not be delivered."""


class AlertCallError(AlertGateError):
    """The model call for one item failed outright, twice."""


@dataclass(frozen=True)
class AlertItem:
    raw_item_id: int
    source_slug: str
    external_id: str
    url: str
    title: str
    body: str
    route: str
    eligible_at: str
    selector_reasons: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    published_at: str | None = None
    published_at_source: str | None = None
    # Why the body could not be fetched, for a title-gate keep behind a paywall.
    body_unavailable: str | None = None


@dataclass(frozen=True)
class AlertDecision:
    item: AlertItem
    potential_alert: bool
    summary_zh: str
    input_tokens: int = 0
    output_tokens: int = 0
    fail_open: bool = False
    error: str | None = None


@dataclass(frozen=True)
class PendingAlert:
    decision_ids: tuple[int, ...]
    source_slug: str
    external_id: str
    url: str
    title: str
    summary_zh: str
    client_name: str
    # Same string the model saw (published_label), not recomputed.
    published_label: str = "unknown"


@dataclass
class AlertGateResult:
    prompt_version: str
    since: str
    until: str
    eligible: int = 0
    offered: list[AlertItem] = field(default_factory=list)
    decisions: list[AlertDecision] = field(default_factory=list)
    pending: list[PendingAlert] = field(default_factory=list)
    emailed: int = 0
    email_message_id: str | None = None
    pushed: int = 0
    dry_run: bool = False
    # Offered items left undecided for the next run, with the error for each.
    skipped: list[tuple[AlertItem, str]] = field(default_factory=list)
    # Why deciding stopped before every offered item was tried, if it did.
    stopped: str | None = None

    @property
    def positives(self) -> int:
        return sum(decision.potential_alert for decision in self.decisions)

    @property
    def fail_open(self) -> int:
        return sum(decision.fail_open for decision in self.decisions)


Sender = Callable[[Sequence[PendingAlert]], str]
Notifier = Callable[[Sequence[PendingAlert]], int]


def taxonomy_path(profile: ClientProfile) -> Path:
    return ROOT / "clients" / profile.slug / TAXONOMY_NAME


def load_taxonomy(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AlertConfigError(f"cannot read alert taxonomy {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AlertConfigError(f"invalid alert taxonomy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AlertConfigError(f"{path} must contain a JSON object")
    alert_types = data.get("alert_types")
    if not isinstance(alert_types, list) or not alert_types:
        raise AlertConfigError(f"{path} has no alert_types")
    return data


def _taxonomy_lines(taxonomy: dict) -> list[str]:
    lines = []
    for entry in taxonomy["alert_types"]:
        identifier = entry.get("id")
        terms = [term for language in ("en", "de")
                 for term in entry.get(language, ()) if isinstance(term, str) and term.strip()]
        if not identifier or not terms:
            raise AlertConfigError("every alert_type needs an id and English or German terms")
        lines.append(f"- {identifier}: {', '.join(terms)}")
    return lines


def render_system_prompt(profile: ClientProfile, taxonomy: dict) -> str:
    about = profile.prompt.about if profile.prompt else profile.name
    own = ", ".join(profile.brands_with_role("own")) or profile.name
    # Named so "a key customer" below is a list, not the model's guess: without
    # it, an EU review of JD.com's Ceconomy bid was alerted as customer news.
    customers = ", ".join(profile.brands_with_role("customer")) or "none listed"
    return f"""You screen already-relevant German and European news for {profile.name}.

Client: {about}
Own-brand names: {own}
Key customers: {customers}

The article has already passed a relevance gate and contains an own-brand name or
one of these alert concepts:
{chr(10).join(_taxonomy_lines(taxonomy))}

Answer potential_alert=true when a human monitoring {profile.name} should look at
this now as a possible alert. This includes any genuine article mention of the own
brand, even when neutral or brief, and a concrete current sensitive event affecting
the client, a key customer, or the parcel market. Prefer recall: the
recipient is a human reviewer, so an uncertain but plausible alert should be sent.

Answer false when the own-brand text is a different entity or incidental boilerplate,
or when the matched alert word is merely ambiguous, hypothetical, generic advice,
a survey called an investigation, historical background, or an event outside the
client's monitored business.

Sometimes the message says BODY: not obtainable. Then the headline passed a
relevance gate but the article text sits behind a paywall, and no own-brand name or
alert concept was matched. Judge on the title and source alone, with the same
preference for recall.

The message gives TODAY and the article's PUBLISHED date, which is often unknown.
When it is unknown, use a date the article states for itself, such as a press
release dateline. Answer false when the article is clearly more than
{STALE_DAYS} days older than TODAY: an old article is not a current alert, even when
a crawler has only just found it. Without any date to go on, judge on content.

When true, write a concise Chinese summary of what is happening in 2-4 sentences.
State claims as reporting (for example, 'the source reports') unless the source is
an official authority. Do not invent facts, urgency, recommendations, or a risk
rating. When false, summary_zh must be an empty string.""".strip()


def prompt_version(system: str) -> str:
    digest = hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]
    return f"{STAGE}-{KIND}-{digest}"


def openai_caller(model: str = ALERT_GATE_MODEL,
                  effort: str = ALERT_GATE_REASONING) -> Caller:
    return _openai_caller(model=model, effort=effort, text_format=REPLY_FORMAT)


def parse_reply(text: str) -> tuple[bool, str] | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"potential_alert", "summary_zh"}:
        return None
    potential, summary = data["potential_alert"], data["summary_zh"]
    if not isinstance(potential, bool) or not isinstance(summary, str):
        return None
    summary = summary.strip()
    if potential and not summary:
        return None
    if not potential and summary:
        return None
    return potential, summary


def published_label(item: AlertItem) -> str:
    """The stored publication date for the model, or "unknown".

    ``lastmod`` is a change signal, not a publication date (CLAUDE.md), and a
    restamped archive would make an old article look current, so it reads as
    unknown here too.
    """
    if not item.published_at or item.published_at_source in (None, "lastmod"):
        return "unknown"
    return f"{item.published_at[:10]} ({item.published_at_source})"


def berlin_day(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone(BERLIN).date().isoformat()


def render_item(item: AlertItem, today: str,
                body_chars: int = BODY_GATE_BODY_CHARS) -> str:
    # The date lives here rather than in the system prompt, whose hash is the
    # prompt version: a daily-changing prompt would version every day's decisions.
    head = (f"TODAY: {today}\nPUBLISHED: {published_label(item)}\n"
            f"SOURCE: {item.source_slug}\nTITLE: {item.title}\nURL: {item.url}\n")
    if item.body_unavailable is not None:
        return (head + f"MATCHED: {', '.join(item.triggers) or 'none'}\n\n"
                f"BODY: not obtainable ({item.body_unavailable}); judge on the title alone.")
    return head + f"MATCHED: {', '.join(item.triggers)}\n\n{item.body[:body_chars]}"


def _decide(system: str, item: AlertItem, caller: Caller, today: str,
            body_chars: int = BODY_GATE_BODY_CHARS) -> AlertDecision:
    """Ask twice at most.

    Two replies in the wrong shape give a flagged fail-open alert: the model
    answered, so asking again next run would likely get the same. A call that
    failed outright the last time raises AlertCallError and stays undecided.
    """
    error = ""
    call_failed = False
    tokens_in = tokens_out = 0
    for _attempt in range(MAX_ATTEMPTS):
        try:
            reply, usage = caller(system, render_item(item, today, body_chars))
            error, call_failed = "", False
        except GateConfigError:
            raise
        except Exception as exc:
            reply, usage = "", {}
            error, call_failed = f"{type(exc).__name__}: {exc}"[:300], True
        tokens_in += usage.get("in", 0)
        tokens_out += usage.get("out", 0)
        parsed = None if error else parse_reply(reply)
        if parsed is not None:
            potential, summary = parsed
            return AlertDecision(item, potential, summary, tokens_in, tokens_out)
        if not error:
            error = f"unusable reply: {reply[:200]!r}"
    if call_failed:
        raise AlertCallError(f"could not decide {item.url}: {error}")
    return AlertDecision(item, True, FAIL_OPEN_SUMMARY, tokens_in, tokens_out,
                         fail_open=True, error=error)


def _payload(value: str) -> dict:
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _latest_news_rows(conn) -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
            "SELECT * FROM raw_item WHERE source_kind='news' "
            "ORDER BY source_slug, external_id, version"):
        latest[(row["source_slug"], row["external_id"])] = dict(row)
    return latest


def _in_window(value: str, since: str, until: str) -> bool:
    # All three values come from utcnow/start_run and use the same sortable ISO
    # representation. Inclusive start is safe because decided identities are
    # removed below; it avoids losing two events stamped in the same second.
    return since <= value <= until


def eligible_news_items(db_path: Path, profile: ClientProfile, since: str,
                        until: str) -> list[AlertItem]:
    """News admitted since ``since`` through body gate or title-gate body route."""
    conn = connect(db_path)
    try:
        latest = _latest_news_rows(conn)
        items: dict[tuple[str, str], AlertItem] = {}

        gates: dict[tuple[str, str], dict] = {}
        for row in conn.execute(
                "SELECT a.*, r.source_slug, r.external_id FROM assessment a "
                "JOIN raw_item r ON r.id=a.raw_item_id "
                "WHERE a.client_slug=? AND r.source_kind='news' "
                "AND substr(a.prompt_version,1,15)='body_gate-news-' "
                "ORDER BY a.created_at, a.id", (profile.slug,)):
            gates[(row["source_slug"], row["external_id"])] = dict(row)

        for key, gate in gates.items():
            if gate["relevant"] not in (1, None) or not _in_window(
                    gate["created_at"], since, until):
                continue
            raw = latest.get(key)
            if not raw:
                continue
            payload = _payload(raw["payload"])
            body = (payload.get("body_text") or "").strip()
            if not body:
                continue
            gate_payload = _payload(gate["payload"])
            items[key] = AlertItem(
                raw_item_id=raw["id"], source_slug=raw["source_slug"],
                external_id=raw["external_id"], url=raw["url"] or key[1],
                title=(raw["title"] or payload.get("title") or raw["url"] or key[1]),
                body=body, route="body_gate", eligible_at=gate["created_at"],
                selector_reasons=tuple(gate_payload.get("selector_reasons") or ()),
                published_at=raw["published_at"],
                published_at_source=payload.get("published_at_source"),
            )

        for fetch in conn.execute(
                "SELECT * FROM body_fetch WHERE status IN ('ok','unavailable')"):
            key = (fetch["source_slug"], fetch["external_id"])
            raw = latest.get(key)
            if not raw:
                continue
            hint = _payload(fetch["hint_payload"])
            # Only a title-gate keep qualifies: a full-text source whose body is
            # unavailable never passed any gate.
            route = (hint.get("title_gate_routes") or {}).get(profile.slug)
            if not isinstance(route, dict):
                continue
            payload = _payload(raw["payload"])
            if fetch["status"] == "unavailable":
                # Eligible from the moment the fetch gave up, so only keeps that
                # become unavailable from now on are offered.
                if key in items or not _in_window(fetch["attempted_at"] or "", since, until):
                    continue
                items[key] = AlertItem(
                    raw_item_id=raw["id"], source_slug=raw["source_slug"],
                    external_id=raw["external_id"], url=raw["url"] or key[1],
                    title=(raw["title"] or payload.get("title") or route.get("label")
                           or raw["url"] or key[1]),
                    body="", route=UNAVAILABLE_ROUTE, eligible_at=fetch["attempted_at"],
                    selector_reasons=tuple(route.get("reasons") or ()),
                    published_at=raw["published_at"],
                    published_at_source=payload.get("published_at_source"),
                    body_unavailable=fetch["error"] or "unavailable",
                )
                continue
            body = (payload.get("body_text") or "").strip()
            if not body or not _in_window(raw["fetched_at"], since, until):
                continue
            items[key] = AlertItem(
                raw_item_id=raw["id"], source_slug=raw["source_slug"],
                external_id=raw["external_id"], url=raw["url"] or key[1],
                title=(raw["title"] or payload.get("title") or raw["url"] or key[1]),
                body=body, route="title_gate_body", eligible_at=raw["fetched_at"],
                selector_reasons=tuple(route.get("reasons") or ()),
                published_at=raw["published_at"],
                published_at_source=payload.get("published_at_source"),
            )
        return sorted(items.values(), key=lambda item: (item.eligible_at, item.raw_item_id))
    finally:
        conn.close()


def _own_rules(profile: ClientProfile) -> tuple[Rule, ...]:
    names = set(profile.brands_with_role("own"))
    return tuple(rule for rule in profile.rules
                 if rule.group == "brand" and rule.name in names)


def _alert_patterns(taxonomy: dict) -> tuple[tuple[str, re.Pattern[str]], ...]:
    patterns = []
    for entry in taxonomy["alert_types"]:
        fragments = []
        for language in ("en", "de"):
            for term in entry.get(language, ()):
                # The customer supplied stems in ordinary singular form. Opening
                # the final suffix catches German plurals such as Bußgelder and
                # Sanktionen; semantic false hits are what the LLM is here to reject.
                fragments.append(compile_term(term.rstrip("*") + "*"))
        patterns.append((entry["id"], re.compile("|".join(fragments), re.IGNORECASE)))
    return tuple(patterns)


def with_triggers(item: AlertItem, profile: ClientProfile,
                  taxonomy: dict) -> AlertItem | None:
    text = normalized(f"{item.title}\n{item.body}")
    triggers: list[str] = []
    own_reasons = {f"brand:{name}" for name in profile.brands_with_role("own")}
    if own_reasons.intersection(item.selector_reasons) or any(
            rule.pattern.search(text) for rule in _own_rules(profile)):
        triggers.append("own_brand")
    triggers.extend(identifier for identifier, pattern in _alert_patterns(taxonomy)
                    if pattern.search(text))
    if not triggers:
        return None
    return replace(item, triggers=tuple(dict.fromkeys(triggers)))


def _already_decided(conn, profile: ClientProfile, version: str) -> set[tuple[str, str]]:
    return {(row["source_slug"], row["external_id"]) for row in conn.execute(
        "SELECT source_slug, external_id FROM alert_decision "
        "WHERE client_slug=? AND prompt_version=? AND profile_version=?",
        (profile.slug, version, profile.profile_version))}


def _store_decision(conn, decision: AlertDecision, profile: ClientProfile,
                    version: str, model: str) -> None:
    item = decision.item
    payload = {
        "stage": STAGE, "kind": KIND, "route": item.route,
        "triggers": list(item.triggers), "selector_reasons": list(item.selector_reasons),
        "model": model, "published_label": published_label(item),
        "fail_open": decision.fail_open, "error": decision.error,
        "tokens": {"in": decision.input_tokens, "out": decision.output_tokens},
    }
    if item.body_unavailable is not None:
        payload["body"] = "unavailable"
        payload["body_unavailable_reason"] = item.body_unavailable
    conn.execute(
        "INSERT OR IGNORE INTO alert_decision "
        "(raw_item_id,source_slug,external_id,client_slug,prompt_version,profile_version,"
        "created_at,potential_alert,summary_zh,payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (item.raw_item_id, item.source_slug, item.external_id, profile.slug, version,
         profile.profile_version, utcnow(), int(decision.potential_alert),
         decision.summary_zh, json.dumps(payload, ensure_ascii=False)))


def pending_alerts(db_path: Path, profile: ClientProfile) -> list[PendingAlert]:
    conn = connect(db_path)
    try:
        rows = list(conn.execute(
            "SELECT d.id,d.source_slug,d.external_id,d.summary_zh,d.payload,r.url,r.title "
            "FROM alert_decision d JOIN raw_item r ON r.id=d.raw_item_id "
            "WHERE d.client_slug=? AND d.potential_alert=1 AND d.sent_at IS NULL "
            "ORDER BY d.created_at,d.id", (profile.slug,)))
    finally:
        conn.close()

    # A deliberate re-gate under a new prompt must not create two sections for
    # the same source identity in one digest. Mark every represented row sent.
    grouped: dict[tuple[str, str], PendingAlert] = {}
    for row in rows:
        key = (row["source_slug"], row["external_id"])
        previous = grouped.get(key)
        ids = (*previous.decision_ids, row["id"]) if previous else (row["id"],)
        payload = _payload(row["payload"])
        title = row["title"] or row["url"] or row["external_id"]
        if payload.get("body") == "unavailable":
            title = f"{title} {BODY_UNAVAILABLE_MARK}"
        grouped[key] = PendingAlert(
            decision_ids=ids, source_slug=row["source_slug"],
            external_id=row["external_id"], url=row["url"] or row["external_id"],
            title=title, summary_zh=row["summary_zh"], client_name=profile.name,
            published_label=payload.get("published_label") or "unknown",
        )
    return list(grouped.values())


def _required_env(name: str, *fallbacks: str) -> str:
    for key in (name, *fallbacks):
        value = os.getenv(key, "").strip()
        if value:
            return value
    raise AlertConfigError(f"{name} is not set; it belongs in .env on the laptop")


def _defeat_autolink(text: str) -> str:
    """Break a bare domain like "example.de" so a client's automatic link
    detector (Apple Mail, Outlook, Gmail, ntfy...) doesn't turn the source
    slug into a second, misleading link — only the explicit URL should
    navigate. A zero-width space around every "." defeats the "word.word"
    pattern those detectors match on without changing how the text looks."""
    zwsp = "​"
    return text.replace(".", f"{zwsp}.{zwsp}")


def build_email(alerts: Sequence[PendingAlert], *, sender: str, recipient: str,
                today: date | None = None) -> EmailMessage:
    day = (today or date.today()).isoformat()
    count = len(alerts)
    message = EmailMessage()
    message["Subject"] = f"[brandmonitor] {count} potential alert{'s' if count != 1 else ''} — {day}"
    message["From"] = f"Brand Monitor <{sender}>"
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])

    plain = [f"{count} potential alert{'s' if count != 1 else ''} require review."]
    blocks = []
    for number, alert in enumerate(alerts, 1):
        plain.append(
            f"{number}. [{alert.client_name}] {alert.title}\n\n{alert.summary_zh}\n\n"
            f"Published: {alert.published_label}\n"
            f"Source: {_defeat_autolink(alert.source_slug)}\nURL: {alert.url}")
        blocks.append(
            f"<h2>{number}. [{html.escape(alert.client_name)}] {html.escape(alert.title)}</h2>"
            f"<p>{html.escape(alert.summary_zh)}</p>"
            f"<p><strong>Published:</strong> {html.escape(alert.published_label)}<br>"
            f"<strong>Source:</strong> {_defeat_autolink(html.escape(alert.source_slug))}<br>"
            f"<strong>URL:</strong> <a href=\"{html.escape(alert.url, quote=True)}\">"
            f"{html.escape(alert.url)}</a></p>")
    message.set_content("\n\n".join(plain) + "\n")
    message.add_alternative(
        f"<html><body><p>{count} potential alert{'s' if count != 1 else ''} "
        f"require review.</p>{''.join(blocks)}</body></html>", subtype="html")
    return message


def smtp_sender() -> Sender:
    from dotenv import load_dotenv

    load_dotenv()
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise AlertConfigError("SMTP_PORT must be an integer") from exc
    sender_address = _required_env("EMAIL_SENDER")
    recipient = _required_env("ALERT_EMAIL_RECIPIENT", "EMAIL_RECIPIENT")
    recipients = [item.strip() for item in recipient.split(",") if item.strip()]
    if not recipients:
        raise AlertConfigError("EMAIL_RECIPIENT is empty")
    username = os.getenv("SMTP_USERNAME", sender_address).strip()
    password = _required_env("SMTP_PASSWORD").replace(" ", "")

    def send(alerts: Sequence[PendingAlert]) -> str:
        message = build_email(alerts, sender=sender_address, recipient=recipient)
        try:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(username, password)
                server.send_message(message, to_addrs=recipients)
        except (OSError, smtplib.SMTPException) as exc:
            raise AlertDeliveryError(f"SMTP delivery failed: {exc}") from exc
        return str(message["Message-ID"])

    return send


def clip_utf8(text: str, max_bytes: int) -> str:
    """Cut text to at most max_bytes of UTF-8 on a character boundary, marking the cut."""
    text = text.strip()
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    cut = text.encode("utf-8")[:max_bytes - len("…".encode("utf-8"))]
    return cut.decode("utf-8", errors="ignore").rstrip() + "…"


def build_pushes(alerts: Sequence[PendingAlert]) -> list[dict]:
    """One push per alert, capped at PUSH_CAP. The URL is a bare line in the
    message body rather than an action button — ntfy auto-linkifies a plain
    URL into a tappable blue link, which reads better than a button. The
    source slug next to it goes through _defeat_autolink so ntfy's own
    detector doesn't also linkify it — only the URL should navigate.

    ntfy.sh turns a message over 4,096 bytes into an attachment; Chinese is three
    bytes a character, so the summary is clipped well below that rather than
    trusting the prompt's 2-4 sentences. The title is prefixed with the client
    name so a reviewer watching one ntfy topic for several clients can tell at a
    glance which one an alert is for; the article title is clipped to whatever
    budget remains after that prefix so the total still fits PUSH_TITLE_BYTES.
    """
    pushes = []
    for alert in alerts[:PUSH_CAP]:
        prefix = f"[{alert.client_name}] "
        title_budget = max(PUSH_TITLE_BYTES - len(prefix.encode("utf-8")), 0)
        pushes.append({
            "title": prefix + clip_utf8(alert.title, title_budget),
            "message": (
                f"{clip_utf8(alert.summary_zh, PUSH_SUMMARY_BYTES)}\n\n"
                f"Published: {alert.published_label}\n"
                f"{_defeat_autolink(alert.source_slug)}\n{alert.url}"
            ),
            "priority": 3,
            "tags": ["newspaper"],
        })
    if len(alerts) > PUSH_CAP:
        pushes.append({
            "title": f"+{len(alerts) - PUSH_CAP} more potential alerts",
            "message": f"{len(alerts)} alerts in this run; the email has all of them.",
            "priority": 4,
            "tags": ["rotating_light"],
        })
    return pushes


def ntfy_notifier() -> Notifier | None:
    """Push each emailed alert to ntfy, or None without NTFY_TOPIC.

    On public ntfy.sh the topic name is the only access control, and the server
    keeps messages for about twelve hours. Email stays the record; a failed push
    is logged and never marks, retries, or blocks anything.
    """
    from dotenv import load_dotenv

    load_dotenv()
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return None
    server = (os.getenv("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
    token = os.getenv("NTFY_TOKEN", "").strip()

    def notify(alerts: Sequence[PendingAlert]) -> int:
        sent = 0
        for push in build_pushes(alerts):
            body = json.dumps({"topic": topic, **push}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                server, data=body, method="POST",
                headers={"Content-Type": "application/json"})
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read()
            except (OSError, ValueError) as exc:  # ValueError: malformed NTFY_SERVER
                logger.warning("alert push failed: %s", exc)
                continue
            sent += 1
        return sent

    return notify


def _mark_sent(db_path: Path, alerts: Sequence[PendingAlert], message_id: str) -> None:
    ids = [identifier for alert in alerts for identifier in alert.decision_ids]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with session(db_path) as conn:
        conn.execute(
            f"UPDATE alert_decision SET sent_at=?,email_message_id=? "
            f"WHERE id IN ({placeholders})",
            (utcnow(), message_id, *ids))


def _initial_since(db_path: Path, end: str, scope: str) -> str:
    with session(db_path) as conn:
        saved = get_watermark(conn, scope)
        if saved:
            return saved
        row = conn.execute(
            "SELECT started_at FROM run WHERE kind='news' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["started_at"] if row else end


def run_alert_gate(profile: ClientProfile, *, db_path: Path = DB_PATH,
                   caller: Caller | None = None, sender: Sender | None = None,
                   notifier: Notifier | None = None,
                   taxonomy_file: Path | None = None, now: str | None = None,
                   dry_run: bool = False, model: str = ALERT_GATE_MODEL) -> AlertGateResult:
    taxonomy = load_taxonomy(taxonomy_file or taxonomy_path(profile))
    system = render_system_prompt(profile, taxonomy)
    version = prompt_version(system)
    end = now or utcnow()
    scope = f"{WATERMARK_PREFIX}:{profile.slug}:alert-news"
    since = _initial_since(db_path, end, scope)
    eligible = eligible_news_items(db_path, profile, since, end)

    conn = connect(db_path)
    try:
        decided = _already_decided(conn, profile, version)
    finally:
        conn.close()
    offered = []
    for item in eligible:
        if (item.source_slug, item.external_id) in decided:
            continue
        triggered = with_triggers(item, profile, taxonomy)
        if triggered:
            offered.append(triggered)
        elif item.body_unavailable is not None:
            offered.append(item)

    result = AlertGateResult(version, since, end, eligible=len(eligible),
                             offered=offered, dry_run=dry_run)
    if dry_run:
        return result

    if offered and caller is None:
        caller = openai_caller(model=model)
    if offered:
        assert caller is not None
    today = berlin_day(end)
    answered = False
    config_errors = failures_in_a_row = 0
    for item in offered:
        try:
            decision = _decide(system, item, caller, today)
        except (AlertCallError, GateConfigError) as exc:
            message = (str(exc) if isinstance(exc, AlertCallError)
                       else f"{type(exc).__name__}: {exc}"[:300])
            result.skipped.append((item, message))
            logger.warning("alert gate left %s undecided: %s", item.url, message)
            # openai_caller reports a refused request as a configuration error.
            # Once any call in this run has been answered, the key and model are
            # fine, and a refusal is about this item - a content filter, say.
            config_errors += isinstance(exc, GateConfigError)
            if not answered and config_errors >= 2:
                result.stopped = f"configuration error: {message}"
                break
        else:
            answered = True
            with session(db_path) as conn:
                _store_decision(conn, decision, profile, version, model)
            result.decisions.append(decision)
            if not decision.fail_open:
                failures_in_a_row = 0
                continue
            logger.warning("alert gate could not decide %s, sent as a flagged alert: %s",
                           item.url, decision.error)
        failures_in_a_row += 1
        if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
            result.stopped = f"{failures_in_a_row} items in a row got no usable answer"
            break

    # The watermark moves only when every candidate in this time slice has a
    # durable decision; otherwise the next run re-offers the undecided ones.
    # Delivery is independent either way: unsent positives are selected without
    # the watermark, so what was decided goes out now.
    if result.stopped is None and not result.skipped:
        with session(db_path) as conn:
            advance_watermark(conn, scope, end)

    result.pending = pending_alerts(db_path, profile)
    if result.pending:
        deliver = sender or smtp_sender()
        message_id = deliver(result.pending)
        _mark_sent(db_path, result.pending, message_id)
        result.emailed = len(result.pending)
        result.email_message_id = message_id
        # Only the real SMTP path pushes by default, so an injected test sender
        # never reaches the network through the environment's NTFY_TOPIC.
        notify = notifier or (ntfy_notifier() if sender is None else None)
        if notify is not None:
            result.pushed = notify(result.pending)
    return result
