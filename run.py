#!/usr/bin/env python3
"""brandmonitor entry point.

Commands:
    probe     Crawl one site's discovery methods and draft its source JSON entry.
    migrate   Apply pending SQLite migrations.
    collect   Run collection over a source list and store raw items.
    collect-dsa   Store DSA Transparency Database daily aggregates per platform.
    collect-safety-gate   Store EU Safety Gate product alerts from weekly XML.
    fetch-bodies  Enrich stored hints and retry failed public-page fetches.
    gate      Ask a cheap LLM which new title-only candidates are worth a body fetch.
    body-gate Ask a cheap LLM whether each selected body is a signal for the client.
    backup    Snapshot the database, compress, rotate, and copy off-box.
    status    Show the last runs and per-source outcomes.

Run `python run.py <command> --help` for per-command options.

Exit codes are a process-level health signal, not a report on what was found:

    0   the command ran to completion. Individual sources may have failed and
        individual items may be unavailable; read the summary or `status`.
    1   the command ran but produced nothing usable — every source failed.
    2   the command could not start or aborted: bad configuration, a missing
        credential, an unreadable source list, or an unhandled exception.

Paywalls, missing pages and unsupported media are expected item outcomes and
never change the exit code. A code that is always non-zero carries no signal,
and whether a run is actually healthy is a question about several runs, not
one — that judgement belongs to a check that reads `run` and `run_source`.
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
    # Individual source and body failures are visible in the summary and in
    # run_source; they recover on the next run from their own watermark. Only a
    # run where nothing at all succeeded is a failure of the command itself.
    return 1 if s["failed"] and not s["ok"] else 0


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
    # A batch where every single fetch failed points at the transport or the
    # environment rather than at the pages; a mix of outcomes is normal.
    return 1 if summary["attempted"] and not summary["ok"] else 0


# A news collection run older than this is not "today's". On a daily schedule it
# means today's collect never started, and gating yesterday's run again would
# only repeat yesterday's decisions.
GATE_MAX_RUN_AGE_HOURS = 20


def cmd_gate(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from src.collect import DEFAULT_NEWS_SOURCES
    from src.config import TITLE_GATE_BATCH_SIZE, TITLE_GATE_MODEL
    from src.db import DB_PATH, migrate, session
    from src.logger import install_excepthook, set_pipeline_log
    from src.profile import load_profile
    from src.title_gate import (GateConfigError, gate_candidates, item_label, openai_caller,
                                render_batch, render_system_prompt, run_gate)

    log_path = set_pipeline_log(f"gate_{args.client}")
    install_excepthook()
    migrate()
    profile = load_profile(args.client)

    run_id = None
    if args.all:
        scope = "every stored title-only item"
    else:
        with session() as conn:
            if args.run:
                row = conn.execute("SELECT id, kind, started_at FROM run WHERE id = ?",
                                   (args.run,)).fetchone()
                if row is None or row["kind"] != "news":
                    print(f"run {args.run} is not a news collection run")
                    return 2
            else:
                row = conn.execute("SELECT id, kind, started_at FROM run WHERE kind = 'news' "
                                   "ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    print("no news collection run recorded yet - nothing to gate")
                    return 0
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["started_at"])
                if age.total_seconds() > GATE_MAX_RUN_AGE_HOURS * 3600:
                    print(f"latest news collection run {row['id']} started "
                          f"{row['started_at'][:16]}, over {GATE_MAX_RUN_AGE_HOURS} hours "
                          f"ago - nothing new to gate. Pass --run {row['id']} to gate it anyway.")
                    return 0
        run_id = row["id"]
        scope = f"collection run {run_id} ({row['started_at'][:16]})"

    sources = Path(args.sources) if args.sources else DEFAULT_NEWS_SOURCES
    to_gate, with_body = gate_candidates(profile, DB_PATH, sources,
                                         None if args.all else {run_id})
    print(f"title gate: {profile.slug}, {scope}")
    print(f"{len(to_gate) + len(with_body)} title-only candidates: {len(to_gate)} to gate, "
          f"{len(with_body)} already have a body and skip the gate")

    if args.dry_run:
        print(f"\n--- system prompt ---\n{render_system_prompt(profile)}")
        for start in range(0, len(to_gate), TITLE_GATE_BATCH_SIZE):
            batch = to_gate[start:start + TITLE_GATE_BATCH_SIZE]
            print(f"\n--- batch {start // TITLE_GATE_BATCH_SIZE + 1} ---\n{render_batch(batch)}")
        return 0
    if not to_gate:
        return 0

    try:
        result = run_gate(to_gate, profile, openai_caller(), run_id=run_id)
    except GateConfigError as exc:
        print(f"title gate could not run: {exc}")
        return 2

    print(f"kept {len(result.kept)}, dropped {len(result.dropped)}; "
          f"{result.failed_batches} of {result.batches} batch(es) kept whole "
          "after unusable replies")
    print(f"model {TITLE_GATE_MODEL}, {result.prompt_version}, profile "
          f"{profile.profile_version}; tokens {result.input_tokens} in, "
          f"{result.output_tokens} out")
    for candidate in result.kept:
        print(f"  keep  {candidate.source_slug:<18} {item_label(candidate)[:100]}")
    print(f"Decisions: {result.log_path}")
    print(f"Log: {log_path}")
    # Kept-whole batches are the gate failing safe; only a run where no batch got
    # a usable answer means the model side is down.
    return 1 if result.failed_batches == result.batches else 0


def cmd_body_gate(args: argparse.Namespace) -> int:
    from src.body_gate import (KINDS, GateConfigError, item_label, openai_caller, pending_items,
                               prompt_version, render_item, render_system_prompt, run_body_gate)
    from src.config import BODY_GATE_MODEL
    from src.db import DB_PATH, migrate
    from src.logger import install_excepthook, set_pipeline_log
    from src.profile import load_profile

    log_path = set_pipeline_log(f"body_gate_{args.client}")
    install_excepthook()
    migrate()
    profile = load_profile(args.client)
    caller = None
    worst = 0
    # Tiers run independently: a profile missing the regulatory inputs must not
    # stop news, and the other way round.
    for kind in (KINDS if args.kind == "all" else (args.kind,)):
        print(f"\nbody gate, {kind}: {profile.slug}, profile {profile.profile_version}")
        try:
            system = render_system_prompt(profile, kind)
        except GateConfigError as exc:
            print(f"  cannot run: {exc}")
            worst = 2
            continue
        version = prompt_version(kind, system)
        items, already = pending_items(profile, kind, DB_PATH,
                                       regate_from=version if args.regate else None)
        deferred = items[args.limit:]
        items = items[:args.limit]
        print(f"  {len(items) + len(deferred)} selected bodies to gate, {already} already "
              f"gated{' at this version' if args.regate else ''}"
              f"{f'; {len(deferred)} deferred by limit' if deferred else ''}")
        if args.dry_run:
            print(f"\n--- system prompt, {version} ---\n{system}")
            if items:
                print(f"\n--- first item ---\n{render_item(items[0], kind)[:1500]}")
            continue
        if not items:
            continue
        try:
            caller = caller or openai_caller()
            result = run_body_gate(items, profile, kind, caller, db_path=DB_PATH)
        except GateConfigError as exc:
            # A key or model problem applies to every tier alike.
            print(f"  body gate could not run: {exc}")
            return 2
        print(f"  relevant {result.count('relevant')}, unsure {result.count('unsure')}, "
              f"irrelevant {result.count('irrelevant')}; {result.fail_open} kept after "
              f"failed calls")
        print(f"  model {BODY_GATE_MODEL}, {result.prompt_version}; tokens "
              f"{result.input_tokens} in, {result.output_tokens} out")
        for decision in result.decisions:
            if decision.verdict != "irrelevant":
                candidate = decision.item.candidate
                print(f"  {decision.verdict:<9} {candidate.source_slug:<24} "
                      f"{item_label(candidate)[:64]:<64} | {decision.reason or decision.error}")
        if result.stopped:
            print(f"  stopped early - {result.stopped}. The rest stays ungated for the next run.")
        # Fail-open items are the gate failing safe; only a tier where no call got
        # a usable answer means the model side is down.
        if result.decisions and result.fail_open == len(result.decisions):
            worst = max(worst, 1)
    print(f"\nDecisions are stored in the assessment table. Log: {log_path}")
    return worst


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
    return 1 if s["failed"] and not s["ok"] else 0


def cmd_collect_safety_gate(args: argparse.Namespace) -> int:
    from src.db import migrate
    from src.logger import install_excepthook, set_pipeline_log, set_verbose
    from src.safety_gate import SafetyGateError, run_safety_gate_collection

    log_path = set_pipeline_log("collect_safety_gate")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()
    try:
        s = run_safety_gate_collection(
            weeks=args.weeks, end=args.end, lookback_weeks=args.lookback,
            max_reports=args.max_reports,
        )
    except SafetyGateError as exc:
        print(f"Safety Gate collection could not start: {exc}")
        return 2

    if s.get("note") == "up to date":
        print(f"nothing to collect: official reports complete through {s['end']}")
        return 0

    print(f"\nrun {s['run_id']}  {s['start']} -> {s['end']}")
    print(f"{s['reports_ok']} report(s) fetched, "
          f"{s['reports_failed']} failed; {s['found']} alerts found, "
          f"{s['stored']} newly stored")
    if s["failed_reports"]:
        print(f"incomplete reports (will be retried): "
              f"{', '.join(s['failed_reports'])}")
    print(f"watermark: {s.get('watermark_advanced') or 'not advanced'}")
    print(f"Log: {log_path}")
    return 1 if s["reports_failed"] and not s["reports_ok"] else 0


def cmd_backup(args: argparse.Namespace) -> int:
    from src.backup import run_backup
    from src.logger import install_excepthook, set_pipeline_log

    log_path = set_pipeline_log("backup")
    install_excepthook()

    # None means "use config"; "" means "explicitly disabled for this run".
    s = run_backup(offbox_dir="" if args.no_offbox else args.offbox)

    print(f"snapshot {s.snapshot} ({s.size_bytes / 1048576:.2f} MB)")
    if s.offbox:
        print(f"off-box  {s.offbox}")
    elif s.offbox_error:
        # The local snapshot is already on disk, so a dead sync target is a
        # warning about the next disaster, not a failed backup today.
        print(f"off-box  FAILED: {s.offbox_error}")
    else:
        print("off-box  not configured")
    print(f"retained {s.kept} snapshots, {s.total_bytes / 1048576:.0f} MB total")
    if s.pruned_local:
        print(f"pruned   {len(s.pruned_local)}: {', '.join(s.pruned_local)}")
    if s.pruned_offbox:
        print(f"pruned off-box {len(s.pruned_offbox)}")
    if s.over_warn_limit:
        print("WARNING: backup directory is over its configured size limit")
    print(f"Log: {log_path}")
    return 0


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
    from src.config import BODY_FETCH_LIMIT, BODY_GATE_LIMIT

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
                    "failed and resumes from its own watermark; a failed source "
                    "does not make successful sources repeat its older window.",
    )
    collect.add_argument("--kind", choices=("news", "regulatory"), default="news",
                         help="which source list to run (default: news)")
    collect.add_argument("--sources", default=None,
                         help="override the source JSON path")
    collect.add_argument("--days", type=float, default=None,
                         help="window in days; default resumes each source's watermark")
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

    gate = sub.add_parser(
        "gate",
        help="cheap LLM check on new title-only candidates",
        description="Run the keyword selector over title-only news sources, then ask a "
                    "small model which headlines are worth a body fetch. By default it "
                    "gates the articles first stored by the latest news collection run. "
                    "Items that already have a body skip it. Needs OPENAI_API_KEY in "
                    ".env; decisions go to data/title_gate/<client>/<date>.jsonl.",
    )
    gate.add_argument("--client", default="jt-express",
                      help="client profile slug or path (default: jt-express)")
    which = gate.add_mutually_exclusive_group()
    which.add_argument("--run", type=positive_int, default=None,
                       help="gate the items first stored by this news collection run, "
                            "to redo a day")
    which.add_argument("--all", action="store_true",
                       help="gate every stored title-only candidate (after a backfill)")
    gate.add_argument("--sources", default=None, help="override the news source JSON path")
    gate.add_argument("--dry-run", action="store_true",
                      help="print the prompt and batches without calling the model")
    gate.set_defaults(func=cmd_gate)

    body_gate = sub.add_parser(
        "body-gate",
        help="cheap LLM relevance check on selected bodies",
        description="Run the keyword selector over the news and regulatory sources, then "
                    "ask a small model whether each selected body without a decision is a "
                    "signal for the client: relevant, unsure or irrelevant. Only irrelevant "
                    "stops an item before the full assessment. Decisions are stored in the "
                    "assessment table under a body_gate-<kind>-<hash> prompt version. Needs "
                    "OPENAI_API_KEY in .env.",
    )
    body_gate.add_argument("--client", default="jt-express",
                           help="client profile slug or path (default: jt-express)")
    body_gate.add_argument("--kind", choices=("news", "regulatory", "all"), default="all",
                           help="which tier to gate (default: all)")
    body_gate.add_argument("--limit", type=positive_int, default=BODY_GATE_LIMIT,
                           help="maximum bodies per tier, newest first (default: config.json)")
    body_gate.add_argument("--regate", action="store_true",
                           help="also re-gate bodies decided under another prompt or "
                                "profile version")
    body_gate.add_argument("--dry-run", action="store_true",
                           help="print the prompts and counts without calling the model")
    body_gate.set_defaults(func=cmd_body_gate)

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

    safety_gate = sub.add_parser(
        "collect-safety-gate",
        help="collect EU Safety Gate weekly product alerts",
        description="Fetch the official weekly-report XML and store every alert "
                    "as a native structured record. The first run defaults to "
                    "the latest 12 reports; later runs resume from a report "
                    "watermark. No credential is required.",
    )
    safety_gate.add_argument(
        "--weeks", type=positive_int, default=None,
        help="deliberately refetch the newest N reports (for backfill or validation)",
    )
    safety_gate.add_argument(
        "--end", type=iso_date, default=None,
        help="with --weeks, use only reports published on/before YYYY-MM-DD",
    )
    safety_gate.add_argument(
        "--lookback", type=positive_int, default=12,
        help="reports to fetch on the first run with no watermark (default: 12)",
    )
    safety_gate.add_argument(
        "--max-reports", type=positive_int, default=None,
        help="process at most N reports, oldest first, so a large backfill can be batched",
    )
    safety_gate.add_argument("--verbose", action="store_true")
    safety_gate.set_defaults(func=cmd_collect_safety_gate)

    backup = sub.add_parser(
        "backup",
        help="snapshot the database, compress, rotate, and copy off-box",
        description="Take an online SQLite snapshot - safe while collection is "
                    "writing - then VACUUM it, gzip it, prune older snapshots to "
                    "the configured daily/weekly/monthly tiers, and copy the "
                    "result to the off-box directory. Retention and paths come "
                    "from config.json.",
    )
    backup.add_argument("--offbox", default=None,
                        help="override the off-box directory for this run")
    backup.add_argument("--no-offbox", action="store_true",
                        help="write the local snapshot only")
    backup.set_defaults(func=cmd_backup)

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
    try:
        return args.func(args)
    except Exception as exc:
        # Anything that escapes a command aborted it: a missing credential, an
        # unreadable source list, a locked database, a defect. The scheduled
        # wrapper needs to tell that apart from a run that completed with some
        # sources down, so it exits 2 rather than the interpreter's own 1.
        from src.logger import get_logger

        get_logger("uncaught").exception("%s aborted", args.command)
        print(f"\n{args.command} aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
