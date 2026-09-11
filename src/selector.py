"""Deterministic candidate selection: which stored items are worth an LLM call.

This is the prefilter, not the assessment layer. It never calls an LLM, fetches a
page, or writes to the database.

The rules are a **client profile** in ``clients/<slug>/profile.json``; this module
holds none of its own. Collection is source-specific, analysis is client-specific,
and this is the analysis side of that rule.

Matching is a plain OR over brands, topics and keywords. It runs against the
title, the URL slug, and — where the body is already stored — the body text.
That last one matters: selecting on a headline has a recall cost, because a brand
can appear in paragraph twelve and never in the title. For the 23 full_text
sources the body is on disk, so using it costs nothing and removes the risk
entirely. Only the nine title_only sources are exposed, and there the exposure is
real: ZEIT ships a title for 21 of 3,753 URLs, so selection there is effectively
slug-only.

``--no-bodies`` forces title/slug matching even where a body exists. That is how
the recall cost of the title_only tier gets measured: run both over the full_text
corpus and compare.

Run from the repository root:

    python -m src.news_selector --client jt-express
    python -m src.news_selector --client jt-express --scope all
    python -m src.news_selector --client jt-express --no-bodies --json

The default scope is the mainstream sources configured as ``title_only``. ``all``
is useful for measuring the same rules against trade press and associations.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from src.config import INPUT_DIR
from src.db import DB_PATH
from src.profile import ClientProfile, load_profile
from vendor.newscrawler.crawler import url_matches_dirs


@dataclass(frozen=True)
class Candidate:
    source_slug: str
    title: str | None
    url: str
    published_at: str | None
    reasons: tuple[str, ...]
    matched_in: tuple[str, ...]


@dataclass(frozen=True)
class MetadataUnknown:
    source_slug: str
    source_kind: str
    url: str
    published_at: str | None


@dataclass(frozen=True)
class SelectionResult:
    candidates: tuple[Candidate, ...]
    unknowns: tuple[MetadataUnknown, ...]
    eligible: int


def normalized(value: str) -> str:
    """Normalize title or slug while keeping characters meaningful to rules."""
    value = html.unescape(unquote(value))
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"[-_/]+", " ", value)
    return " ".join(value.split())


GENERIC_PATH_SEGMENTS = frozenset({
    "aktuell", "aktuelles", "article", "articles", "artikel", "beitrag",
    "beitraege", "blog", "content", "de", "detail", "details", "download",
    "downloads", "en", "home", "index", "magazin", "meldung", "meldungen",
    "news", "node", "page", "presse", "pressemitteilung",
    "pressemitteilungen", "publication", "publications", "seite", "stories",
    "story", "thema", "themen",
})

WEB_SUFFIX = re.compile(r"\.(?:html?|php|aspx?|pdf)$", re.IGNORECASE)
PDF_SUFFIX = re.compile(r"\.pdf(?:[?#].*)?$", re.IGNORECASE)


def _meaningful_path_segment(segment: str) -> str:
    """Return readable segment text, or empty text for routing and opaque IDs."""
    value = WEB_SUFFIX.sub("", unquote(segment)).strip()
    text = normalized(value)
    lowered = text.casefold()
    if not text or lowered in GENERIC_PATH_SEGMENTS:
        return ""

    # Match Unicode words while excluding digits and underscores.  Requiring a
    # meaningful three-character word drops numeric CMS IDs, one-letter slugs and
    # routes such as ``meldungen-2023`` whose only word is generic.
    words = re.findall(r"[^\W\d_]+", lowered, flags=re.UNICODE)
    if not any(len(word) >= 3 and word not in GENERIC_PATH_SEGMENTS for word in words):
        return ""
    return text


def pdf_filename_text(url: str) -> str:
    """Return a decoded PDF filename carried in a query parameter, if present."""
    for values in parse_qs(urlparse(url).query).values():
        for value in values:
            filename = unquote(value).replace("\\", "/").rsplit("/", 1)[-1]
            if PDF_SUFFIX.search(filename):
                return normalized(PDF_SUFFIX.sub("", filename))
    return ""


def url_metadata(url: str) -> tuple[str, str | None]:
    """Return the best body-free URL label and the field it came from.

    Some download endpoints keep the real PDF filename in ``?file=``.  Some news
    sites put a numeric CMS ID in the final path segment and the headline-like
    slug immediately before it.  Walk backwards, but never inspect the hostname.
    """
    filename = pdf_filename_text(url)
    if filename:
        return filename, "pdf_filename"
    segments = [part for part in urlparse(url).path.split("/") if part]
    for segment in reversed(segments):
        text = _meaningful_path_segment(segment)
        if text:
            return text, "slug"
    return "", None


def slug_text(url: str) -> str:
    """Return the best meaningful URL label; never inspect the hostname."""
    return url_metadata(url)[0]


def select_candidate(title: str | None, url: str, profile: ClientProfile,
                     body: str | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (reasons, fields) for an item, or two empty tuples when unmatched.

    Title and slug match on plain OR. The body is stricter - see
    ClientProfile.body_reasons - because the same rule means less in 1,500 words
    than in a headline: measured, a single broad keyword in a body selected 116
    items of which almost none were useful.
    """
    title_text = normalized(title or "")
    url_text, url_field = url_metadata(url)
    title_reasons = profile.reasons_for(title_text)
    url_reasons = profile.reasons_for(url_text)
    body_reasons = profile.body_reasons(normalized(body or "")) if body else ()

    reasons = tuple(dict.fromkeys((*title_reasons, *url_reasons, *body_reasons)))
    fields = tuple(name for name, matches in
                   (("title", title_reasons), (url_field, url_reasons),
                    ("body", body_reasons))
                   if name is not None and matches)
    return reasons, fields


def configured_sources(sources_path: Path, mode: str) -> dict[str, dict]:
    entries = json.loads(sources_path.read_text(encoding="utf-8"))
    return {
        urlparse(entry["url"]).netloc.lower().removeprefix("www."): entry
        for entry in entries
        if mode == "all" or entry.get("content_mode", "title_only") == mode
    }


def _discovery_title(row: sqlite3.Row) -> str | None:
    """Use titles known without reading a body.

    Body enrichment can fill an originally missing title.  Those titles must not
    make this pre-fetch experiment look better than it is, so a version carrying
    ``body_text`` is not a discovery-title source.
    """
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    title = row["title"]
    return title.strip() if title and title.strip() and not payload.get("body_text") else None


def _payload(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}


def _stored_body(row: sqlite3.Row) -> str | None:
    """Body text already fetched for this item, if any."""
    body = _payload(row).get("body_text")
    return body if isinstance(body, str) and body.strip() else None


def _body_title(row: sqlite3.Row) -> str | None:
    """The title body enrichment recovered.

    _discovery_title deliberately refuses these so the pre-fetch measurement
    stays honest. When bodies are in play there is no such pretence, and a
    recovered title is the best label we have - it is how a bare-URL source like
    BVL becomes readable at all.
    """
    if not _stored_body(row):
        return None
    title = row["title"]
    return title.strip() if title and title.strip() else None


def selection_from_db(db_path: Path,
                      source_specs: Sequence[tuple[Path, str]],
                      scope: str,
                      profile: ClientProfile,
                      source_filters: Sequence[str] = (),
                      use_bodies: bool = True) -> SelectionResult:
    """Select latest source items and identify rows with no usable metadata."""
    mode = "title_only" if scope == "title-only" else "all"
    configured: dict[tuple[str, str], dict] = {}
    for sources_path, source_kind in source_specs:
        configured.update({(source_kind, slug): entry for slug, entry in
                           configured_sources(sources_path, mode).items()})
    if source_filters:
        wanted = {value.lower().removeprefix("www.") for value in source_filters}
        configured = {key: entry for key, entry in configured.items()
                      if key[1] in wanted}

    kinds = sorted({kind for kind, _slug in configured})
    if not kinds:
        return SelectionResult((), (), 0)
    placeholders = ",".join("?" for _kind in kinds)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            f"SELECT * FROM raw_item WHERE source_kind IN ({placeholders}) "
            "ORDER BY source_slug, external_id, version", kinds
        ))
    finally:
        conn.close()

    # Keep the newest raw version, but carry forward the newest non-empty title
    # that discovery itself supplied.  This prevents a later bare sitemap hint
    # from erasing a title previously obtained from a feed or news sitemap.
    latest: dict[tuple[str, str, str], sqlite3.Row] = {}
    titles: dict[tuple[str, str, str], str] = {}
    for row in rows:
        key = (row["source_kind"], row["source_slug"], row["external_id"])
        latest[key] = row
        title = _discovery_title(row)
        if title:
            titles[key] = title

    eligible: list[tuple[sqlite3.Row, str | None]] = []
    for key, row in latest.items():
        entry = configured.get((row["source_kind"], row["source_slug"]))
        if entry is None:
            continue
        try:
            discovered_via = json.loads(row["payload"]).get("discovered_via")
        except (TypeError, ValueError):
            discovered_via = None
        # Match collection/body semantics: allowed_dirs is an output filter only
        # for sitemap discovery. Feeds ignore it and frontpage uses it as a seed.
        if (discovered_via == "sitemap" and
                not url_matches_dirs(row["url"], entry.get("allowed_dirs"))):
            continue
        eligible.append((row, titles.get(key)))
    eligible.sort(key=lambda item: (
        item[0]["source_slug"], item[0]["published_at"] or item[0]["fetched_at"]),
        reverse=True)

    selected: list[Candidate] = []
    unknowns: list[MetadataUnknown] = []
    for row, title in eligible:
        url_text, _url_field = url_metadata(row["url"])
        if use_bodies and not title:
            title = _body_title(row)
        if not title and not url_text and not (use_bodies and _stored_body(row)):
            unknowns.append(MetadataUnknown(
                source_slug=row["source_slug"],
                source_kind=row["source_kind"],
                url=row["url"],
                published_at=row["published_at"],
            ))
            continue
        body = _stored_body(row) if use_bodies else None
        reasons, fields = select_candidate(title, row["url"], profile, body)
        if reasons:
            selected.append(Candidate(
                source_slug=row["source_slug"],
                title=title,
                url=row["url"],
                published_at=row["published_at"],
                reasons=reasons,
                matched_in=fields,
            ))
    return SelectionResult(tuple(selected), tuple(unknowns), len(eligible))


def candidates_from_db(db_path: Path, sources_path: Path, scope: str,
                       source_filters: Sequence[str] = (),
                       profile: ClientProfile = None, *,
                       source_kind: str = "news") -> tuple[list[Candidate], int]:
    """Backward-compatible selection helper for one source configuration."""
    result = selection_from_db(
        db_path, ((sources_path, source_kind),), scope, profile, source_filters)
    return list(result.candidates), result.eligible


def format_report(candidates: Iterable[Candidate], eligible: int,
                  unknowns: Iterable[MetadataUnknown] = ()) -> str:
    items = list(candidates)
    unknown_items = list(unknowns)
    lines = [f"Selected {len(items)} of {eligible} latest items "
             f"({(100 * len(items) / eligible if eligible else 0):.2f}%); "
             f"{len(unknown_items)} have no usable title or URL label."]
    by_source: dict[str, list[Candidate]] = {}
    for item in items:
        by_source.setdefault(item.source_slug, []).append(item)
    for source, source_items in by_source.items():
        lines.append(f"\n{source}: {len(source_items)}")
        for item in source_items:
            label = item.title or slug_text(item.url)
            lines.append(f"  [{','.join(item.matched_in)}] {label}")
            lines.append(f"      {', '.join(item.reasons)}")
            lines.append(f"      {item.url}")
    if unknown_items:
        lines.append(f"\nMetadata unknown: {len(unknown_items)}")
        for item in unknown_items:
            lines.append(f"  [{item.source_kind}] {item.source_slug}: {item.url}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help=f"SQLite corpus (default: {DB_PATH})")
    parser.add_argument("--kind", choices=("news", "regulatory", "all"), default="news",
                        help="source kind to inspect (default: news)")
    parser.add_argument("--sources", type=Path, default=None,
                        help="override source configuration (not valid with --kind all)")
    parser.add_argument("--scope", choices=("title-only", "all"), default=None,
                        help="content-mode tier (default: title-only for news; all otherwise)")
    parser.add_argument("--source", action="append", default=[],
                        help="limit to a source slug; repeatable")
    parser.add_argument("--client", default="jt-express",
                        help="client profile slug or path (default: jt-express)")
    parser.add_argument("--no-bodies", action="store_true",
                        help="match titles and slugs only, even where a body is "
                             "stored; use this to measure the recall cost of the "
                             "title_only tier")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.sources and args.kind == "all":
        parser.error("--sources cannot be combined with --kind all")
    scope = args.scope or ("title-only" if args.kind == "news" else "all")
    defaults = {
        "news": INPUT_DIR / "germany_medias.json",
        "regulatory": INPUT_DIR / "regulatory_sources.json",
    }
    if args.kind == "all":
        specs = tuple((path, kind) for kind, path in defaults.items())
    else:
        specs = ((args.sources or defaults[args.kind], args.kind),)
    result = selection_from_db(
        args.db, specs, scope, load_profile(args.client), args.source,
        use_bodies=not args.no_bodies)
    if args.json:
        print(json.dumps({"eligible": result.eligible,
                          "selected": len(result.candidates),
                          "unknown": len(result.unknowns),
                          "candidates": [asdict(item) for item in result.candidates],
                          "unknowns": [asdict(item) for item in result.unknowns]},
                         ensure_ascii=False, indent=2))
    else:
        print(format_report(result.candidates, result.eligible, result.unknowns))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
