#!/usr/bin/env python3
"""Independently reconcile a few critical publisher endpoints with SQLite.

This is an observer, not another collector. It never writes the database and it
does not reuse the crawler's discovery parsers. Findings are data in the emitted
snapshot; exit 2 is reserved for the observer itself being unable to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from health.common import ROOT, format_utc, now_utc, publish_snapshot  # noqa: E402


DEFAULT_CONFIG = ROOT / "health" / "canaries.json"
DEFAULT_DB = ROOT / "data" / "brandmonitor.sqlite3"
DEFAULT_OUTPUT = ROOT / "data" / "health" / "canaries"
UTC = timezone.utc
USER_AGENT = "BrandMonitorHealth/1.0 (+publisher coverage canary)"
CHALLENGE_MARKERS = (
    b"just a moment",
    b"cf-chl-",
    b"cloudflare ray id",
    b"attention required! | cloudflare",
    b"enable javascript and cookies to continue",
)
SEVERITY_RANK = {"healthy": 0, "warning": 1, "critical": 2}


class CanaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fetched:
    url: str
    status_code: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class ObservedItem:
    url: str
    published_at: str | None
    sort_time: datetime | None
    order: int


Fetcher = Callable[[str, int, int], Fetched]


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"mc_cid", "mc_eid"}
    ]
    return urlunsplit(
        parts._replace(
            scheme=(parts.scheme or "https").lower(),
            netloc=parts.netloc.lower(),
            path=parts.path or "/",
            query=urlencode(query, doseq=True),
            fragment="",
        )
    )


def _read_limited(response: requests.Response, maximum: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > maximum:
        raise CanaryError(f"response declares {declared} bytes; limit is {maximum}")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(65536):
        total += len(chunk)
        if total > maximum:
            raise CanaryError(f"response exceeded {maximum} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def http_fetch(url: str, timeout: int, maximum: int) -> Fetched:
    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,application/rss+xml,application/atom+xml,*/*;q=0.1"},
            timeout=(5, timeout),
            allow_redirects=True,
            stream=True,
        ) as response:
            response.raise_for_status()
            body = _read_limited(response, maximum)
            return Fetched(
                response.url,
                response.status_code,
                response.headers.get("Content-Type", ""),
                body,
            )
    except requests.RequestException as exc:
        raise CanaryError(f"{type(exc).__name__}: {exc}") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_xml(body: bytes, endpoint: str) -> tuple[str, list[ObservedItem]]:
    lowered = body[:250000].lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        raise CanaryError("response is an access-challenge page")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise CanaryError(f"response is not valid XML: {exc}") from exc

    root_name = _local_name(root.tag)
    items: list[ObservedItem] = []
    if root_name in {"urlset", "sitemapindex"}:
        node_name = "url" if root_name == "urlset" else "sitemap"
        for order, node in enumerate(child for child in root if _local_name(child.tag) == node_name):
            url = _child_text(node, "loc")
            if not url:
                continue
            published = _child_text(node, "publication_date") or _child_text(node, "lastmod")
            items.append(ObservedItem(url, published, _parse_time(published), order))
        return root_name, items

    if root_name == "rss":
        for order, node in enumerate(child for child in root.iter() if _local_name(child.tag) == "item"):
            url = _child_text(node, "link") or _child_text(node, "guid")
            if not url:
                continue
            published = _child_text(node, "pubdate") or _child_text(node, "date")
            items.append(ObservedItem(url, published, _parse_time(published), order))
        return "feed", items

    if root_name == "feed":
        for order, node in enumerate(child for child in root if _local_name(child.tag) == "entry"):
            url = None
            for child in node:
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    url = child.attrib["href"].strip()
                    if child.attrib.get("rel", "alternate") == "alternate":
                        break
            url = url or _child_text(node, "id")
            if not url:
                continue
            published = _child_text(node, "published") or _child_text(node, "updated")
            items.append(ObservedItem(url, published, _parse_time(published), order))
        return "feed", items

    raise CanaryError(f"unexpected XML root <{root_name}> from {endpoint}")


def _page_number(url: str) -> int:
    values = parse_qs(urlsplit(url).query).get("page", [])
    try:
        return int(values[-1]) if values else -1
    except ValueError:
        return -1


def _choose_index_children(items: list[ObservedItem], mode: str) -> list[ObservedItem]:
    if not items:
        return []
    if mode == "highest_page":
        return [max(items, key=lambda item: (_page_number(item.url), item.order))]
    if mode == "first":
        return [items[0]]
    raise CanaryError(f"unsupported follow_sitemap_index value {mode!r}")


def _fetch_items(check: dict[str, Any], fetcher: Fetcher, timeout: int, maximum: int) -> tuple[list[ObservedItem], list[dict[str, Any]]]:
    endpoints: list[dict[str, Any]] = []
    first = fetcher(check["url"], timeout, maximum)
    root_kind, items = _parse_xml(first.body, first.url)
    endpoints.append({
        "url": first.url,
        "status_code": first.status_code,
        "content_type": first.content_type,
        "bytes": len(first.body),
        "document_kind": root_kind,
    })
    if root_kind == "sitemapindex":
        mode = check.get("follow_sitemap_index")
        if not mode:
            raise CanaryError("endpoint is a sitemap index but no follow mode is configured")
        combined: list[ObservedItem] = []
        for child in _choose_index_children(items, mode):
            response = fetcher(child.url, timeout, maximum)
            child_kind, child_items = _parse_xml(response.body, response.url)
            if child_kind != "urlset":
                raise CanaryError(f"followed sitemap resolved to {child_kind}, not urlset")
            endpoints.append({
                "url": response.url,
                "status_code": response.status_code,
                "content_type": response.content_type,
                "bytes": len(response.body),
                "document_kind": child_kind,
            })
            combined.extend(child_items)
        items = combined
    expected = "feed" if check["kind"] == "feed" else "urlset"
    actual = root_kind if root_kind != "sitemapindex" else ("urlset" if items else "sitemapindex")
    if actual != expected:
        raise CanaryError(f"expected {expected}, received {actual}")
    return items, endpoints


def _path_allowed(url: str, check: dict[str, Any]) -> bool:
    path = urlsplit(url).path or "/"
    includes = check.get("include_path_prefixes") or []
    excludes = check.get("exclude_path_prefixes") or []
    return (not includes or any(path.startswith(prefix) for prefix in includes)) and not any(
        path.startswith(prefix) for prefix in excludes
    )


def _recent(items: list[ObservedItem], count: int) -> list[ObservedItem]:
    unique: dict[str, ObservedItem] = {}
    for item in items:
        unique.setdefault(canonical_url(item.url), item)
    values = list(unique.values())
    values.sort(
        key=lambda item: (
            item.sort_time is not None,
            item.sort_time or datetime.min.replace(tzinfo=UTC),
            -item.order,
        ),
        reverse=True,
    )
    return values[:count]


def _stored_urls(conn: sqlite3.Connection, source_slug: str) -> set[str]:
    return {
        canonical_url(row[0])
        for row in conn.execute(
            "SELECT external_id FROM raw_item WHERE source_slug = ?", (source_slug,)
        )
    }


def _issue(check: dict[str, Any], kind: str, severity: str, message: str) -> dict[str, str]:
    return {
        "source": check["source_slug"],
        "check": f"canary:{check['id']}:{kind}",
        "severity": severity,
        "message": message,
    }


def _settled(items: list[ObservedItem], check: dict[str, Any], now: datetime) -> list[ObservedItem]:
    """Drop entries dated within ``ignore_newer_than_hours`` of now, or in the future.

    An article listed an hour ago may simply not have been collected yet, and a
    scheduled article can carry a publication date hours ahead.
    """
    hours = check.get("ignore_newer_than_hours")
    if not hours:
        return items
    cutoff = now - timedelta(hours=float(hours))
    return [item for item in items if item.sort_time is None or item.sort_time <= cutoff]


def run_check(
    check: dict[str, Any], conn: sqlite3.Connection, *, fetcher: Fetcher,
    timeout: int, maximum: int, now: datetime | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    try:
        items, endpoints = _fetch_items(check, fetcher, timeout, maximum)
        eligible = [item for item in items if _path_allowed(item.url, check)]
        minimum = int(check.get("minimum_entries", 1))
        if len(eligible) < minimum:
            issues.append(_issue(
                check,
                "structure",
                check.get("entry_count_severity", "warning"),
                f"parsed {len(eligible)} eligible entries; expected at least {minimum}",
            ))
        recent = _recent(_settled(eligible, check, now or now_utc()),
                         int(check.get("reconcile_recent", 10)))
        stored = _stored_urls(conn, check["source_slug"])
        present = [item for item in recent if canonical_url(item.url) in stored]
        missing = [item.url for item in recent if canonical_url(item.url) not in stored]
        ratio = len(present) / len(recent) if recent else 0.0
        threshold = float(check.get("minimum_database_coverage", 0.8))
        if recent and ratio < threshold:
            issues.append(_issue(
                check,
                "database_coverage",
                check.get("coverage_severity", "warning"),
                f"database contains {len(present)}/{len(recent)} recent endpoint URLs "
                f"({ratio:.0%}); expected at least {threshold:.0%}",
            ))
        status = max(
            (issue["severity"] for issue in issues),
            key=lambda value: SEVERITY_RANK[value],
            default="healthy",
        )
        dated = [item for item in eligible if item.sort_time is not None]
        latest = max(dated, key=lambda item: item.sort_time) if dated else None
        return {
            "id": check["id"],
            "source_slug": check["source_slug"],
            "kind": check["kind"],
            "status": status,
            "endpoints": endpoints,
            "eligible_entries": len(eligible),
            "recent_considered": len(recent),
            "recent_in_database": len(present),
            "database_coverage": round(ratio, 4),
            "latest_published_at": latest.published_at if latest else None,
            "missing_urls": missing,
            "incidents": issues,
        }
    except Exception as exc:  # one publisher must not suppress the other observations
        issue = _issue(
            check,
            "transport_or_structure",
            check.get("failure_severity", "critical"),
            f"{type(exc).__name__}: {exc}",
        )
        return {
            "id": check["id"],
            "source_slug": check["source_slug"],
            "kind": check["kind"],
            "status": issue["severity"],
            "endpoints": [],
            "eligible_entries": 0,
            "recent_considered": 0,
            "recent_in_database": 0,
            "database_coverage": 0.0,
            "latest_published_at": None,
            "missing_urls": [],
            "incidents": [issue],
        }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_news_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM run WHERE kind = 'news' AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CanaryError("canary config must be a schema_version 1 object")
    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise CanaryError("canary config needs a non-empty checks list")
    required = {"id", "source_slug", "kind", "url"}
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not required <= check.keys():
            raise CanaryError(f"canary check {index} is missing required fields")
        if check["kind"] not in {"sitemap", "feed"}:
            raise CanaryError(f"canary {check['id']} has unsupported kind {check['kind']!r}")
        for field in ("failure_severity", "coverage_severity", "entry_count_severity"):
            if field in check and check[field] not in {"warning", "critical"}:
                raise CanaryError(f"canary {check['id']} has invalid {field}")
        minimum = int(check.get("minimum_entries", 1))
        recent = int(check.get("reconcile_recent", 10))
        coverage = float(check.get("minimum_database_coverage", 0.8))
        grace = float(check.get("ignore_newer_than_hours", 0))
        if minimum < 0 or recent < 1 or not 0 <= coverage <= 1 or grace < 0:
            raise CanaryError(f"canary {check['id']} has invalid numeric thresholds")
    return raw


def run_canaries(
    *, db_path: Path, config_path: Path, output_dir: Path,
    cycle_date: str, generated_at: datetime | None = None,
    fetcher: Fetcher = http_fetch,
) -> dict[str, Any]:
    config = load_config(config_path)
    generated = generated_at or now_utc()
    with _connect_readonly(db_path) as conn:
        news_run_id = _latest_news_run(conn)
        checks = [
            run_check(
                check,
                conn,
                fetcher=fetcher,
                timeout=int(config.get("timeout_seconds", 20)),
                maximum=int(config.get("max_response_bytes", 12 * 1024 * 1024)),
                now=generated,
            )
            for check in config["checks"]
        ]
    incidents = [incident for check in checks for incident in check["incidents"]]
    status = max(
        (check["status"] for check in checks),
        key=lambda value: SEVERITY_RANK[value],
        default="healthy",
    )
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    cycle_id = f"{cycle_date}-news-{news_run_id if news_run_id is not None else 'none'}"
    snapshot = {
        "schema_version": 1,
        "kind": "publisher_canaries",
        "generated_utc": format_utc(generated),
        "cycle_date": cycle_date,
        "cycle_id": cycle_id,
        "news_run_id": news_run_id,
        "config_sha256": config_hash,
        "status": status,
        "checks": checks,
        "incidents": incidents,
    }
    history, latest = publish_snapshot(output_dir, cycle_id, snapshot)
    snapshot["_paths"] = {"history": str(history), "latest": str(latest)}
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cycle-date", default=date.today().isoformat())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_canaries(
            db_path=args.db,
            config_path=args.config,
            output_dir=args.output_dir,
            cycle_date=args.cycle_date,
        )
    except Exception as exc:
        print(f"ERROR: canary observer failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"[canary] {result['status']}: {len(result['checks'])} checks, "
        f"{len(result['incidents'])} incident(s)"
    )
    for incident in result["incidents"]:
        print(
            f"  {incident['severity']} {incident['source']} "
            f"{incident['check']}: {incident['message']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
