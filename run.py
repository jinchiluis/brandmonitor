#!/usr/bin/env python3
"""brandmonitor entry point.

Commands:
    probe     Crawl one site's discovery methods and draft its source JSON entry.
    migrate   Apply pending SQLite migrations.
    collect   Run collection over a source list and store raw items.
    collect-dsa   Store DSA Transparency Database daily aggregates per platform.
    fetch-bodies  Enrich stored hints and retry failed public-page fetches.
    status    Show the last runs and per-source outcomes.

Run `python run.py <command> --help` for per-command options.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    # Source titles are German and reports are Chinese; cp1252 would raise on both.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def cmd_probe(args: argparse.Namespace) -> int:
    from src.logger import install_excepthook, set_pipeline_log, set_verbose
    from src.probe import format_report, probe_site

    log_path = set_pipeline_log("probe")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    report = probe_site(
        args.site,
        days=args.days,
        max_per_source=args.max,
        frontpage_cap=args.frontpage_cap,
        sources_path=args.sources,
    )
    print()
    print(format_report(report, depth=args.depth, samples=args.samples))
    print(f"\nLog: {log_path}")

    # A site nothing could be discovered on is a failure worth a non-zero exit.
    return 0 if any(r.ok for r in report["results"]) else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    from src.db import DB_PATH, migrate

    applied = migrate()
    if applied:
        for name in applied:
            print(f"applied {name}")
    else:
        print("schema already up to date")
    print(f"database: {DB_PATH}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from src.collect import DEFAULT_NEWS_SOURCES, DEFAULT_REGULATORY_SOURCES, run_collection
    from src.db import migrate
    from src.logger import install_excepthook, set_pipeline_log, set_verbose

    log_path = set_pipeline_log(f"collect_{args.kind}")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()  # cheap, and removes "did you migrate?" as a failure mode
    path = args.sources or (DEFAULT_REGULATORY_SOURCES if args.kind == "regulatory"
                            else DEFAULT_NEWS_SOURCES)

    s = run_collection(path, days=args.days, kind=args.kind,
                       max_per_source=args.max, workers=args.workers,
                       body_limit=args.body_limit)

    print(f"\nrun {s['run_id']}  {s['start'][:16]} -> {s['end'][:16]}")
    print(f"{'source':<28}{'status':>8}{'found':>8}{'stored':>8}")
    print("-" * 54)
    for r in sorted(s["per_source"], key=lambda x: (x["status"], -x["found"])):
        print(f"{(r['organization'] or r['slug'])[:27]:<28}{r['status']:>8}"
              f"{r['found']:>8}{r['stored']:>8}")
        if r["error"]:
            print(f"    -> {r['error'][:96]}")
    print(f"\n{s['ok']} ok, {s['zero']} zero yield, {s['failed']} failed")
    print(f"{s['found']} items found, {s['stored']} newly stored")
    if "bodies" in s:
        print_body_summary(s["bodies"])
    print(f"Log: {log_path}")
    bodies = s.get("bodies", {})
    return 1 if s["failed"] or bodies.get("failed") or bodies.get("unavailable") else 0


def print_body_summary(summary: dict) -> None:
    print(f"\nbody run {summary['run_id']}: {summary['attempted']} attempted, "
          f"{summary['ok']} ok, {summary['stored']} new versions, "
          f"{summary['failed']} failed, {summary['unavailable']} unavailable")
    remaining = summary["remaining"]
    print(f"Backlog: {remaining['pending']} pending, {remaining['failed']} retryable, "
          f"{remaining['unavailable']} unavailable; {summary['deferred']} deferred by limit")


def cmd_fetch_bodies(args: argparse.Namespace) -> int:
    from src.bodies import run_body_fetch
    from src.collect import DEFAULT_NEWS_SOURCES, DEFAULT_REGULATORY_SOURCES
    from src.db import migrate
    from src.logger import install_excepthook, set_pipeline_log

    log_path = set_pipeline_log(f"bodies_{args.kind}")
    install_excepthook()
    migrate()
    path = Path(args.sources) if args.sources else (
        DEFAULT_REGULATORY_SOURCES if args.kind == "regulatory" else DEFAULT_NEWS_SOURCES)
    summary = run_body_fetch(path, kind=args.kind, limit=args.limit,
                             refresh=args.refresh, retry_unavailable=args.retry_unavailable)
    print_body_summary(summary)
    print(f"Log: {log_path}")
    return 1 if summary["failed"] or summary["unavailable"] else 0


def cmd_collect_dsa(args: argparse.Namespace) -> int:
    from src.db import migrate
    from src.dsa import DsaError, run_dsa_collection
    from src.logger import install_excepthook, set_pipeline_log, set_verbose

    log_path = set_pipeline_log("collect_dsa")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()
    try:
        s = run_dsa_collection(args.platforms, days=args.days, end=args.end,
                               lookback_days=args.lookback)
    except DsaError as exc:
        # A missing or rejected token is a configuration problem, not a run that
        # found nothing, so say so plainly instead of reporting an empty run.
        print(f"DSA collection could not start: {exc}")
        return 2

    if s.get("note") == "up to date":
        print(f"nothing to collect: complete through {s['end']}")
        return 0

    print(f"\nrun {s['run_id']}  {s['start']} -> {s['end']}  ({s['days']} days)")
    print(f"{'platform':<20}{'status':>8}{'days':>7}{'stored':>8}")
    print("-" * 43)
    for r in sorted(s["per_source"], key=lambda x: (x["status"], -x["found"])):
        print(f"{r['organization'][:19]:<20}{r['status']:>8}{r['found']:>7}{r['stored']:>8}")
        if r["error"]:
            print(f"    -> {r['error'][:96]}")
    print(f"\n{s['ok']} ok, {s['zero']} zero yield, {s['failed']} failed")
    print(f"{s['stored']} platform-days newly stored")
    if s.get("failed_days"):
        print(f"incomplete days (will be refilled next run): {', '.join(s['failed_days'])}")
    print(f"watermark: {s.get('watermark_advanced') or 'not advanced'}")
    print(f"Log: {log_path}")
    return 1 if s["failed"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    from src.db import session

    with session() as conn:
        runs = list(conn.execute(
            "SELECT * FROM run ORDER BY id DESC LIMIT ?", (args.limit,)))
        if not runs:
            print("no runs recorded yet - try: python run.py collect")
            return 0
        for run in runs:
            print(f"\nrun {run['id']}  {run['kind']:<11}{run['status']:<9}"
                  f"{(run['started_at'] or '')[:19]}  {run['note'] or ''}")
            for r in conn.execute(
                "SELECT * FROM run_source WHERE run_id = ? "
                "ORDER BY status, items_found DESC", (run["id"],)
            ):
                line = (f"    {r['source_slug']:<30}{r['status']:>8}"
                        f"{r['items_found']:>7} found {r['items_stored']:>6} new")
                print(line)
                if r["error"]:
                    print(f"        {r['error'][:92]}")
        total = conn.execute("SELECT COUNT(*) c FROM raw_item").fetchone()["c"]
        print(f"\nraw items stored: {total}")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='body_fetch'").fetchone():
            print("Body fetch states (all queued sources):")
            for row in conn.execute("SELECT status, COUNT(*) n FROM body_fetch GROUP BY status"):
                print(f"    {row['status']:<14}{row['n']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from src.config import BODY_FETCH_LIMIT

    parser = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "probe",
        help="probe one site and draft its source entry",
        description="Probe a single site's discovery methods before adding it to a "
                    "source list. Without --sources the domain is treated as unknown, "
                    "so every method runs with no directory filter.",
    )
    probe.add_argument("site", help="site URL or bare domain, e.g. https://www.zeit.de/")
    probe.add_argument("--days", type=float, default=7.0,
                       help="lookback window in days (default: 7)")
    probe.add_argument("--max", type=int, default=2000,
                       help="max sitemap URLs to collect (default: 2000)")
    probe.add_argument("--frontpage-cap", type=int, default=300,
                       help="max frontpage links to follow (default: 300)")
    probe.add_argument("--depth", type=int, default=1,
                       help="path segments per prefix in the directory table (default: 1)")
    probe.add_argument("--samples", type=int, default=2,
                       help="sample URLs to print per prefix (default: 2)")
    probe.add_argument("--sources", default=None,
                       help="source JSON file to load, which applies that domain's "
                            "existing rules instead of probing it as unknown")
    probe.add_argument("--verbose", action="store_true", help="show crawler debug output")
    probe.set_defaults(func=cmd_probe)

    migrate = sub.add_parser("migrate", help="apply pending SQLite migrations")
    migrate.set_defaults(func=cmd_migrate)

    collect = sub.add_parser(
        "collect",
        help="collect a source list into the database",
        description="Run every enabled discovery method for each configured source "
                    "and store what it finds. Each source reports ok, zero yield, or "
                    "failed; the watermark only advances when none failed.",
    )
    collect.add_argument("--kind", choices=("news", "regulatory"), default="news",
                         help="which source list to run (default: news)")
    collect.add_argument("--sources", default=None,
                         help="override the source JSON path")
    collect.add_argument("--days", type=float, default=None,
                         help="window in days; default resumes from the watermark")
    collect.add_argument("--max", type=int, default=2000,
                         help="max sitemap URLs per source (default: 2000)")
    collect.add_argument("--workers", type=int, default=None,
                         help="parallel sources (default: config.json workers.crawler)")
    collect.add_argument("--body-limit", type=positive_int, default=BODY_FETCH_LIMIT,
                         help="maximum body fetches after discovery (default: config.json)")
    collect.add_argument("--verbose", action="store_true")
    collect.set_defaults(func=cmd_collect)

    bodies = sub.add_parser("fetch-bodies", help="fetch pending bodies from stored hints")
    bodies.add_argument("--kind", choices=("news", "regulatory"), default="news")
    bodies.add_argument("--sources", default=None, help="override the source JSON path")
    bodies.add_argument("--limit", type=positive_int, default=BODY_FETCH_LIMIT,
                        help="maximum fetches (default: config.json)")
    bodies.add_argument("--refresh", action="store_true",
                        help="also recheck successful bodies for content changes")
    bodies.add_argument("--retry-unavailable", action="store_true",
                        help="also retry paywalls, missing pages and unsupported media")
    bodies.set_defaults(func=cmd_fetch_bodies)

    dsa = sub.add_parser(
        "collect-dsa",
        help="collect DSA Transparency Database daily aggregates",
        description="Store one aggregate row per platform per day. Statements are "
                    "not stored individually: the tracked platforms file millions "
                    "per day and territorial_scope:DE covers 63-99 per cent of "
                    "them, so this source is a trend signal, not an item feed. "
                    "Needs DSA_KEY in .env.",
    )
    dsa.add_argument("--platforms", default=None,
                     help="override the platform JSON path "
                          "(default: input/dsa_platforms.json)")
    dsa.add_argument("--days", type=positive_int, default=None,
                     help="window in days ending yesterday; default resumes from "
                          "the watermark")
    dsa.add_argument("--end", type=iso_date, default=None,
                     help="last day to collect, YYYY-MM-DD, for refilling a "
                          "historical window (never later than yesterday)")
    dsa.add_argument("--lookback", type=positive_int, default=30,
                     help="days to collect on a first run with no watermark "
                          "(default: 30)")
    dsa.add_argument("--verbose", action="store_true")
    dsa.set_defaults(func=cmd_collect_dsa)

    status = sub.add_parser("status", help="show recent runs and per-source outcomes")
    status.add_argument("--limit", type=int, default=3, help="runs to show (default: 3)")
    status.set_defaults(func=cmd_status)

    return parser


def iso_date(value: str):
    from datetime import date
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
