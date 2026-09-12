"""Stage 4: deterministic checks that know nothing about the model.

This is what makes fabrication structurally detectable rather than something a
reviewer has to catch. Every check reads its constants from the manifest, so it
generalises to any window - the hand-made experiment hardcoded that week's
counts (``==1004``, ``==464``), which made the checks true exactly once.

What is enforced:

* every decision references an identity in the frozen export;
* every identity carries exactly one decision - a bijection, no gaps;
* every external URL in the report appears in the frozen evidence;
* every fetched_at precedes the window cutoff;
* the census reconciles: gated = passed + stopped + fetched after the cutoff;
* no ``lastmod`` date is presented as a publication date anywhere;
* rendered HTML has no broken local link or anchor, no replacement character,
  and no raw markdown link that failed to render.

A failure exits 1. `cited_but_unread` is reported as a warning rather than an
error: citing an item the assessor never opened is worth seeing every time, but
a restatement merged into a story is a legitimate case of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.logger import get_logger
from src.report_agent.bundle import FROZEN_FILES, Bundle
from src.report_agent.export import TRUSTED_DATE_SOURCES

logger = get_logger(__name__)

RAW_MD_LINK = re.compile(r"\]\(https?://")
# Structured sources assert their own event date through a named field rather
# than through published_at_source, so they are dated by a different rule.
STRUCTURED_PROVENANCE = frozenset({"weekly_bulletin", "submission_date_not_event"})


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("href"):
            self.links.append(attributes["href"])
        if attributes.get("id"):
            self.ids.add(attributes["id"])


def verify_bundle(bundle: Bundle) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest = bundle.manifest

    missing = bundle.missing_frozen()
    if missing:
        errors.append(f"frozen export is incomplete: missing {', '.join(missing)}")

    decisions = bundle.maybe("decisions.json")
    ledger = bundle.maybe("editorial-ledger.json")
    if decisions is None or ledger is None:
        errors.append("no assessment or no render yet: decisions.json / "
                      "editorial-ledger.json are missing")
        return _write_result(bundle, errors, warnings, {})

    # -- the bijection ---------------------------------------------------
    identities = set(bundle.by_id)
    decided = [entry["raw_item_id"] for entry in decisions]
    unknown = sorted(set(decided) - identities)
    duplicated = sorted({raw_id for raw_id in decided if decided.count(raw_id) > 1})
    undecided = sorted(identities - set(decided))
    if unknown:
        errors.append(f"{len(unknown)} decision(s) name an id outside the export: {unknown[:5]}")
    if duplicated:
        errors.append(f"{len(duplicated)} identity/identities decided twice: {duplicated[:5]}")
    if undecided:
        errors.append(f"{len(undecided)} identity/identities have no decision: {undecided[:5]}")

    # -- the cutoff ------------------------------------------------------
    cutoff = manifest["window_end_exclusive"]
    late = [row["id"] for row in bundle.evidence + bundle.stopped
            if row["fetched_at"] >= cutoff]
    if late:
        errors.append(f"{len(late)} frozen row(s) were fetched at or after the cutoff: "
                      f"{late[:5]}")

    # -- census arithmetic -----------------------------------------------
    gated_candidates = sum(row["n"] for row in manifest["counts"]
                           if row["route"] == "body_gate")
    census = (gated_candidates + manifest["stopped_asof_identities"]
              + manifest["gated_identities_without_pre_cutoff_raw"])
    if census != manifest["unique_gated_identities"]:
        errors.append(f"census does not reconcile: {gated_candidates} passed + "
                      f"{manifest['stopped_asof_identities']} stopped + "
                      f"{manifest['gated_identities_without_pre_cutoff_raw']} post-cutoff "
                      f"= {census}, but {manifest['unique_gated_identities']} identities "
                      "were gated")
    if manifest["ledger_identities"] != len(identities):
        errors.append(f"manifest ledger_identities is {manifest['ledger_identities']} "
                      f"but the frozen files hold {len(identities)} identities")

    # -- dates -----------------------------------------------------------
    mislabelled = [row["id"] for row in bundle.evidence + bundle.stopped
                   if row["event_or_publication_day"]
                   and row["date_provenance"] not in TRUSTED_DATE_SOURCES
                   and row["date_provenance"] not in STRUCTURED_PROVENANCE]
    if mislabelled:
        errors.append(f"{len(mislabelled)} row(s) carry a date from an untrusted "
                      f"provenance - a lastmod date must never become a publication "
                      f"date: {mislabelled[:5]}")

    # -- the report's sources --------------------------------------------
    pages: dict[str, _Page] = {}
    for path in sorted(bundle.path.glob("*.html")):
        page = _Page()
        page.feed(path.read_text(encoding="utf-8"))
        pages[path.name] = page

    known_urls = {row["url"] for row in bundle.by_id.values() if row.get("url")}
    known_urls |= {row.get("pdf_url") for row in bundle.maybe("dip_documents.json", [])
                   if row.get("pdf_url")}
    report_page = pages.get("weekly-report.zh.html")
    external: list[str] = []
    if report_page is None:
        errors.append("weekly-report.zh.html was not rendered")
    else:
        external = [url for url in report_page.links if urlsplit(url).scheme]
        for url in external:
            if url not in known_urls:
                errors.append(f"report URL is absent from the frozen evidence: {url}")

    # -- local links, encoding, unrendered markdown ------------------------
    for name, page in pages.items():
        for link in page.links:
            parts = urlsplit(link)
            if parts.scheme:
                continue
            target = parts.path or name
            if not (bundle.path / target).exists():
                errors.append(f"{name}: local link to missing {target}")
            elif parts.fragment and target in pages and parts.fragment not in pages[target].ids:
                errors.append(f"{name}: missing anchor #{parts.fragment} in {target}")
        text = (bundle.path / name).read_text(encoding="utf-8")
        if "�" in text:
            errors.append(f"{name}: contains a replacement character")
        if RAW_MD_LINK.search(text):
            errors.append(f"{name}: contains an unrendered markdown link")
        if text.count("<table>") != text.count("</table>"):
            errors.append(f"{name}: unbalanced table markup")

    # -- what the report cites, and whether it was read -------------------
    depth_of = {row["raw_item_id"]: row["review_depth"] for row in ledger}
    cited_ids = _cited_ids(bundle, external)
    unread = sorted(raw_id for raw_id in cited_ids if depth_of.get(raw_id) == "title_only")
    if unread:
        warnings.append(f"{len(unread)} cited identity/identities were never opened by "
                        f"the assessor: {unread[:8]}")

    # -- the register -----------------------------------------------------
    register = bundle.maybe("issue-register.json", [])
    for issue in register:
        for key in ("baseline", "current"):
            stray = [raw_id for raw_id in issue.get(key, []) if raw_id not in identities]
            if key == "current" and stray:
                errors.append(f"issue {issue['id']}: current id(s) {stray} are not in "
                              "this export")

    summary = {
        "identities": len(identities),
        "decisions": len(decisions),
        "ledger_rows": len(ledger),
        "issues": len(register),
        "report_source_links": len(external),
        "unique_report_source_links": len(set(external)),
        "cited_identities": len(cited_ids),
        "cited_but_unread": unread,
        "bodies_read_in_full": sum(1 for d in depth_of.values() if d == "full_stored_body"),
        "checks": {
            "frozen_export_complete": not missing,
            "decision_bijection": not (unknown or duplicated or undecided),
            "fetched_before_cutoff": not late,
            "census_reconciles": census == manifest["unique_gated_identities"],
            "no_untrusted_dates": not mislabelled,
            "report_urls_in_evidence": bool(report_page) and not any(
                "absent from the frozen evidence" in e for e in errors),
            "local_links_and_anchors": not any("local link" in e or "anchor" in e
                                               for e in errors),
        },
    }
    return _write_result(bundle, errors, warnings, summary)


def _cited_ids(bundle: Bundle, external: list[str]) -> set[int]:
    by_url: dict[str, int] = {}
    for row in bundle.by_id.values():
        if row.get("url"):
            by_url.setdefault(row["url"], row["id"])
    return {by_url[url] for url in external if url in by_url}


def _write_result(bundle: Bundle, errors: list[str], warnings: list[str],
                  summary: dict[str, Any]) -> dict[str, Any]:
    result = {"bundle": bundle.path.name, "client": bundle.client_slug,
              "window": bundle.manifest["display_window"],
              "status": "failed" if errors else "passed",
              **summary, "errors": errors, "warnings": warnings}
    bundle.write("verification.json", result)
    bundle.write("bundle-hashes.json", {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.path.iterdir())
        if path.is_file() and path.name != "bundle-hashes.json"})
    for error in errors:
        logger.error("[verify] %s", error)
    for warning in warnings:
        logger.warning("[verify] %s", warning)
    return result
