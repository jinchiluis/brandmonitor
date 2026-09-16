#!/usr/bin/env python3
"""brandmonitor entry point.

Commands:
    probe     Crawl one site's discovery methods and draft its source JSON entry.
    migrate   Apply pending SQLite migrations.
    record-pass  Record a scheduled batch pass for monitoring; used by the batch files.
    collect   Run collection over a source list and store raw items.
    collect-dsa   Store DSA Transparency Database daily aggregates per platform.
    collect-safety-gate   Store EU Safety Gate product alerts from weekly XML.
    collect-dip   Store Bundestag and Bundesrat procedures from the DIP API.
    collect-ep    Store every EU legislative procedure and its progress.
    fetch-dip-docs  Fetch the documents behind the DIP procedures the body gate kept.
    fetch-bodies  Enrich stored hints and retry failed public-page fetches.
    gate      Ask a cheap LLM which new title-only candidates are worth a body fetch.
    body-gate Ask a cheap LLM whether each selected body is a signal for the client.
    alert-gate Email one digest of potential alerts found in newly admitted news.
    export-window  Freeze one client-week of stored material into a report bundle.
    assess    Assess a frozen bundle: carry-forward, triage, cluster, read, challenge.
    report    Render the Chinese report, editorial ledger and source coverage.
    verify-report  Check a rendered bundle's accounting, dates, links and citations.
    backup    Snapshot the database, compress, rotate, and copy off-box.
    safety-gate-view  Print the client's Safety Gate view; writes nothing.
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

    from src.probe import ALL_METHODS

    methods = ([m.strip() for m in args.methods.split(",") if m.strip()]
               if args.methods else ALL_METHODS)
    report = probe_site(
        args.site,
        days=args.days,
        max_per_source=args.max,
        frontpage_cap=args.frontpage_cap,
        sources_path=args.sources,
        methods=methods,
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


def cmd_netcheck(args: argparse.Namespace) -> int:
    """Report whether this host has internet. Exit 4 means it does not.

    The scheduled batch files run this before anything else and skip the whole
    pass on 4, so an outage produces one clear marker instead of every stage
    failing separately in whichever way its first request happens to fail.
    """
    from src.net import DEFAULT_TIMEOUT, OFFLINE_EXIT, check_online

    status = check_online(args.timeout if args.timeout is not None else DEFAULT_TIMEOUT)
    print(f"{'online' if status.online else 'OFFLINE'}: {status.detail}")
    return 0 if status.online else OFFLINE_EXIT


def cmd_record_pass(args: argparse.Namespace) -> int:
    """Record one batch pass for monitoring (src/monitoring.py).

    Called last by run_daily.bat and run_intraday.bat, which ignore its exit code:
    a pass that could not be recorded is a missing monitoring row, not a failed
    pass. It writes no pipeline log of its own; its output goes to the batch log.
    """
    from src.db import migrate
    from src.monitoring import record_pass

    try:
        migrate()  # an offline pass may be the first command since a deployment
    except Exception as exc:  # noqa: BLE001 - record_pass reports what follows
        print(f"[record-pass] migrate failed: {type(exc).__name__}: {exc}")
    pass_id = record_pass(kind=args.kind, outcome=args.outcome, started=args.started,
                          marker=Path(args.marker) if args.marker else None)
    print(f"[record-pass] {args.kind} {args.outcome}: "
          f"{'pass ' + str(pass_id) if pass_id else 'not recorded'}")
    return 0 if pass_id else 1


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
                       body_limit=args.body_limit, exclude=args.exclude)

    print(f"\nrun {s['run_id']}  {s['start'][:16]} -> {s['end'][:16]}")
    print(f"{'source':<28}{'status':>8}{'found':>8}{'stored':>8}")
    print("-" * 54)
    for r in sorted(s["per_source"], key=lambda x: (x["status"], -x["found"])):
        print(f"{(r['organization'] or r['slug'])[:27]:<28}{r['status']:>8}"
              f"{r['found']:>8}{r['stored']:>8}")
        if r["error"]:
            print(f"    -> {r['error'][:96]}")
    print(f"\n{s['ok']} ok, {s['zero']} zero yield, {s['failed']} failed, "
          f"{s['paused']} paused")
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
    if summary.get("stopped"):
        print(f"Stopped early: {summary['stopped']}")


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
    if args.title_gate_client and args.kind != "news":
        print("--title-gate-client can only be used with --kind news")
        return 2
    summary = run_body_fetch(path, kind=args.kind, limit=args.limit,
                             refresh=args.refresh, retry_unavailable=args.retry_unavailable,
                             title_gate_client=args.title_gate_client, exclude=args.exclude)
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


def cmd_alert_gate(args: argparse.Namespace) -> int:
    from src.alert_gate import AlertConfigError, AlertDeliveryError, run_alert_gate
    from src.db import DB_PATH, migrate
    from src.logger import install_excepthook, set_pipeline_log
    from src.profile import load_profile
    from src.title_gate import GateConfigError

    log_path = set_pipeline_log(f"alert_gate_{args.client}")
    install_excepthook()
    migrate()
    profile = load_profile(args.client)
    try:
        result = run_alert_gate(profile, db_path=DB_PATH, dry_run=args.dry_run)
    except (AlertConfigError, AlertDeliveryError, GateConfigError) as exc:
        print(f"alert gate could not run: {exc}")
        return 2

    print(f"alert gate: {profile.slug}, {result.since[:19]} -> {result.until[:19]}")
    print(f"{result.eligible} newly admitted news bodies; "
          f"{len(result.offered)} matched own-brand or alert terms")
    if args.dry_run:
        for item in result.offered:
            label = ", ".join(item.triggers)
            if item.body_unavailable is not None:
                label = ", ".join(filter(None, (label, "body unavailable")))
            print(f"  [{label}] {item.source_slug}  {item.title[:100]}")
        print("dry run: no model calls, decisions, watermark, or email")
    else:
        print(f"checked {len(result.decisions)}; {result.positives} potential alerts, "
              f"{result.fail_open} of them flagged because the model gave no usable answer")
        for item, error in result.skipped:
            print(f"  undecided, retried next run: {item.url} - {error}")
        if result.emailed:
            print(f"emailed one digest containing {result.emailed} alert(s); "
                  f"{result.pushed} push notification(s) sent")
        else:
            print("no email needed")
        if result.stopped:
            print(f"stopped early - {result.stopped}; the watermark did not advance")
    print(f"Log: {log_path}")
    # An undecided item is retried by the next run, but one that keeps failing
    # needs a person, so it must not look like a clean run.
    return 2 if result.stopped else 1 if result.skipped else 0


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
    from src.safety_gate import run_safety_gate_collection

    log_path = set_pipeline_log("collect_safety_gate")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()
    s = run_safety_gate_collection(
        weeks=args.weeks, end=args.end, lookback_weeks=args.lookback,
        max_reports=args.max_reports,
    )
    if s["aborted"]:
        print(f"run {s['run_id']}  Safety Gate collection could not start: {s['aborted']}")
        return 2

    if s.get("note") == "up to date":
        print(f"run {s['run_id']}  nothing to collect: official reports complete "
              f"through {s['end']}")
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


def cmd_collect_dip(args: argparse.Namespace) -> int:
    from src.db import migrate
    from src.dip import DipKeyError, run_dip_collection
    from src.logger import install_excepthook, set_pipeline_log, set_verbose

    log_path = set_pipeline_log("collect_dip")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()
    try:
        s = run_dip_collection(days=args.days, lookback_days=args.lookback)
    except DipKeyError as exc:
        print(f"DIP collection could not run: {exc}")
        return 2

    print(f"\nrun {s['run_id']}  procedures changed since {s['start']}")
    print(f"{s['listed']} listed: {s['excluded']} procedural types skipped, {s['stale']} "
          f"re-indexed archive skipped (latest step too old for a first sight)")
    print(f"{s['found']} procedures checked, {s['stored']} new versions stored")
    for error in s["errors"][:5]:
        print(f"    -> {error[:120]}")
    print(f"watermark: {s.get('watermark_advanced') or 'not advanced'}")
    print(f"Log: {log_path}")
    return 1 if s["errors"] and not s["found"] else 0


def cmd_collect_ep(args: argparse.Namespace) -> int:
    from src.db import migrate
    from src.ep_procedures import run_ep_collection
    from src.logger import install_excepthook, set_pipeline_log, set_verbose

    log_path = set_pipeline_log("collect_ep")
    install_excepthook()
    if args.verbose:
        set_verbose(True)

    migrate()
    s = run_ep_collection(since_year=args.since_year, sweep=args.sweep)
    print(f"\nrun {s['run_id']}  {s['listed']} procedures listed "
          f"({', '.join(s['types'])} since {s['since_year']}), {s['due']} due"
          f"{' - weekly sweep' if s['swept'] else ''}")
    print(f"{s['fetched']} fetched, {s['stored']} new versions stored, {s['stale']} "
          f"closed before first sight and skipped")
    if s["changed"]:
        shown = ", ".join(s["changed"][:12])
        more = len(s["changed"]) - 12
        print(f"changed: {shown}" + (f" and {more} more" if more > 0 else ""))
    if s["final"]:
        print(f"published as law, no longer polled: {', '.join(s['final'][:12])}")
    if s["stopped"]:
        print(f"stopped early: {s['stopped']}")
    if s["unknown"]:
        print(f"{len(s['unknown'])} listed but unknown to the API: {', '.join(s['unknown'])}")
    for error in s["errors"][:5]:
        print(f"    -> {error[:120]}")
    print(f"Log: {log_path}")
    if s["listing_failed"]:
        return 2
    return 1 if s["errors"] and not s["fetched"] else 0


def cmd_fetch_dip_docs(args: argparse.Namespace) -> int:
    from src.db import migrate
    from src.dip import DipKeyError
    from src.dip_documents import DEFAULT_LIMIT, run_document_fetch, show_procedure
    from src.logger import install_excepthook, set_pipeline_log
    from src.profile import load_profile

    log_path = set_pipeline_log(f"dip_docs_{args.client}")
    install_excepthook()
    migrate()
    if args.show:
        shown = show_procedure(args.show)
        if shown is None:
            print(f"no stored DIP procedure {args.show}")
            return 2
        print(shown)
        return 0

    profile = load_profile(args.client)
    try:
        s = run_document_fetch(profile.slug, limit=args.limit or DEFAULT_LIMIT,
                               retry_unavailable=args.retry_unavailable)
    except DipKeyError as exc:
        print(f"DIP documents could not be fetched: {exc}")
        return 2

    reopened = f", {s['reopened']} reopened" if s["reopened"] else ""
    print(f"\nrun {s['run_id']}  {s['kept']} DIP procedures relevant for {profile.slug}; "
          f"{s['queued']} new documents queued{reopened}")
    print(f"{s['attempted']} attempted: {s['ok']} stored, {s['no_text']} without text yet, "
          f"{s['failed']} failed, {s['unavailable']} given up; {s['deferred']} deferred by limit")
    for error in s["errors"][:5]:
        print(f"    -> {error[:120]}")
    r = s["remaining"]
    print(f"Documents: {r['ok']} stored, {r['pending']} pending, {r['failed']} to retry, "
          f"{r['unavailable']} given up")
    print(f"Log: {log_path}")
    # Missing text is DIP's publication lag, not a failure; only a batch where
    # every request failed points at the transport.
    return 1 if s["attempted"] and s["failed"] == s["attempted"] else 0


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
    if s.state:
        print(f"state    {s.state} ({s.state_bytes / 1048576:.2f} MB: "
              f"{', '.join(s.state_dirs)})")
        if s.state_offbox:
            print(f"off-box  {s.state_offbox}")
    elif s.state_error:
        print(f"state    FAILED: {s.state_error}")
    else:
        print("state    nothing to archive")
    print(f"retained {s.kept} snapshots, {s.total_bytes / 1048576:.0f} MB total")
    if s.pruned_local:
        print(f"pruned   {len(s.pruned_local)}: {', '.join(s.pruned_local)}")
    if s.pruned_offbox:
        print(f"pruned off-box {len(s.pruned_offbox)}")
    if s.over_warn_limit:
        print("WARNING: backup directory is over its configured size limit")
    print(f"Log: {log_path}")
    return 0


def cmd_safety_gate_view(args: argparse.Namespace) -> int:
    """Read-only. What the full assessor would be handed for this client."""
    import collections
    from src.safety_gate import select_client_alerts

    # An unreadable profile or database aborts the command through main(), which
    # is exit 2 - the same contract as the gate commands.
    alerts = select_client_alerts(args.client)
    if args.since:
        alerts = [a for a in alerts if (a["published_at"] or "") >= args.since.isoformat()]
    if args.key_customers:
        alerts = [a for a in alerts if a["key_customers"]]
    if not alerts:
        print("no alerts in the client view - try: python run.py collect-safety-gate")
        return 0

    for alert in alerts:
        if args.full:
            print(f"\n{'=' * 78}\n{alert['body_text']}")
            print(f"\nmatched: {', '.join(alert['reasons'])}")
            print(f"raw_item {alert['raw_item_id']}  {alert['url'] or ''}")
            continue
        customers = "+".join(alert["key_customers"]) or "-"
        payload = alert["payload"]
        print(f"{alert['published_at'] or '?':<11}{alert['case_number']:<14}"
              f"{customers:<14}{str(payload.get('product'))[:26]:<27}"
              f"{str(payload.get('riskType'))[:24]}")

    weeks = len({(a["payload"]["report"]["year"], a["payload"]["report"]["week"])
                 for a in alerts})
    exposed = [a for a in alerts if a["key_customers"]]
    named = collections.Counter(name for a in alerts for name in a["key_customers"])
    print(f"\n{len(alerts)} alerts over {weeks} weekly report(s), "
          f"{len(alerts) / weeks:.1f} per week")
    print(f"key-customer exposure: {len(exposed)} "
          f"({', '.join(f'{n} {c}' for n, c in named.most_common()) or 'none'})")
    # Safety Gate never names a carrier, so an own-brand hit would be a defect in
    # the profile rather than a find; see docs/selection_and_assessment.md.
    print("own-brand mentions: 0 by construction - no carrier is named in the record")
    return 0


def _resolve_bundle(args: argparse.Namespace):
    """Accept a bundle path, or --client with --since/--until to name one."""
    from src.report_agent.bundle import Bundle, default_bundle_dir

    if args.bundle:
        return Bundle(args.bundle)
    if not (args.since and args.until):
        raise SystemExit("give --bundle, or --client with --since and --until")
    return Bundle(default_bundle_dir(args.client, args.since, args.until))


def cmd_export_window(args: argparse.Namespace) -> int:
    from src.logger import install_excepthook, set_pipeline_log
    from src.report_agent.export import export_window

    log_path = set_pipeline_log("export-window")
    install_excepthook()

    bundle = export_window(args.client, args.since, args.until, out_dir=args.out)
    manifest = bundle.manifest
    print(f"bundle   {bundle.path}")
    print(f"window   {manifest['display_window']}")
    print(f"candidates {manifest['evidence_items']}  stopped {manifest['stopped_asof_identities']}"
          f"  post-cutoff {manifest['gated_identities_without_pre_cutoff_raw']}")
    for row in manifest["counts"]:
        print(f"    {row['kind']:<12}{row['route']:<24}{row['period']:<9}{row['n']:>5}")
    print(f"accounted identities: {manifest['ledger_identities']}"
          f" (+{manifest['unavailable_title_routes']} unreadable title routes)")
    print(f"Log: {log_path}")
    return 0 if manifest["evidence_items"] else 1


def cmd_assess(args: argparse.Namespace) -> int:
    from src.logger import install_excepthook, set_pipeline_log
    from src.report_agent.assess import load_config, run_assessment

    log_path = set_pipeline_log("assess")
    install_excepthook()

    bundle = _resolve_bundle(args)
    config = load_config()
    result = run_assessment(
        bundle, model=args.model or config.get("model"), config=config,
        timeout=config.get("timeout_seconds", 600),
        max_stories=args.max_stories or config.get("max_stories"),
        skip_write=args.no_write)

    for step in result.steps:
        detail = ", ".join(f"{k}={v}" for k, v in step.items()
                           if k not in ("step", "prompt_version") and v is not None)
        print(f"{step['step']:<15}{detail}")
    errors = [p for p in result.problems if p["severity"] == "error"]
    print(f"\nissues {len(result.register)} ({result.reportable} reportable), "
          f"decisions {len(result.decisions)}")
    print(f"challenge: {len(result.problems)} problem(s), {len(errors)} corrected as errors")
    for problem in errors[:5]:
        print(f"    [{problem['story_id']}] {problem['claim'][:80]}")
    usage = bundle.maybe("assessment-manifest.json", {}).get("usage", {}).get("total", {})
    if usage:
        print(f"tokens in {usage['input_tokens']:,} out {usage['output_tokens']:,} "
              f"over {usage['model_calls']} model calls and {usage['tool_calls']} tool calls")
    print(f"Log: {log_path}")
    return 0 if result.register else 1


def cmd_report(args: argparse.Namespace) -> int:
    from src.logger import install_excepthook, set_pipeline_log
    from src.report_agent.render import render_bundle

    log_path = set_pipeline_log("report")
    install_excepthook()

    bundle = _resolve_bundle(args)
    result = render_bundle(bundle)
    print(f"bundle   {bundle.path}")
    for name in result.written:
        print(f"    {name}")
    print(f"report cites {len(result.cited_ids)} frozen identities")
    if args.record:
        from src.db import session, utcnow

        with session() as conn:
            conn.execute(
                "INSERT INTO report (client_slug, kind, window_start, window_end, "
                "created_at, path, note) VALUES (?, 'weekly', ?, ?, ?, ?, ?)",
                (bundle.client_slug, bundle.manifest["window_start"],
                 bundle.manifest["window_end_exclusive"], utcnow(),
                 str(bundle.path / "weekly-report.zh.html"),
                 f"profile {bundle.manifest['profile_version']}"))
        print("recorded in the report table")
    print(f"Log: {log_path}")
    return 0


def cmd_verify_report(args: argparse.Namespace) -> int:
    from src.logger import install_excepthook, set_pipeline_log
    from src.report_agent.verify import verify_bundle

    log_path = set_pipeline_log("verify-report")
    install_excepthook()

    bundle = _resolve_bundle(args)
    result = verify_bundle(bundle)
    for name, passed in result.get("checks", {}).items():
        print(f"    {'ok  ' if passed else 'FAIL'}  {name}")
    print(f"\n{result['status']}: {result.get('identities', 0)} identities, "
          f"{result.get('ledger_rows', 0)} ledger rows, "
          f"{result.get('unique_report_source_links', 0)} distinct source links")
    for warning in result["warnings"]:
        print(f"warning: {warning}")
    for error in result["errors"]:
        print(f"ERROR: {error}")
    print(f"Log: {log_path}")
    return 1 if result["errors"] else 0


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
    probe.add_argument("--methods", default=None,
                       help="comma-separated subset of sitemap,feeds,frontpage "
                            "(default: all three)")
    probe.add_argument("--sources", default=None,
                       help="source JSON file to load, which applies that domain's "
                            "existing rules instead of probing it as unknown")
    probe.add_argument("--verbose", action="store_true", help="show crawler debug output")
    probe.set_defaults(func=cmd_probe)

    migrate = sub.add_parser("migrate", help="apply pending SQLite migrations")
    migrate.set_defaults(func=cmd_migrate)

    netcheck = sub.add_parser(
        "netcheck",
        help="is this host on the internet? exit 0 yes, 4 no",
        description="Preflight for the scheduled batch files. Resolves and connects to "
                    "a few independent endpoints that are not sources we crawl, and "
                    "reports whether resolution or routing is what failed.",
    )
    netcheck.add_argument("--timeout", type=float, default=None,
                          help="seconds per connection attempt (default: 5)")
    netcheck.set_defaults(func=cmd_netcheck)

    record_pass = sub.add_parser(
        "record-pass",
        help="record one scheduled batch pass for monitoring",
        description="Called by run_daily.bat and run_intraday.bat when a pass ends, "
                    "including offline and lock-skipped slots. Copies the pass marker's "
                    "stage codes and adds the deployed commit and config hashes. Changes "
                    "nothing the pipeline reads.",
    )
    record_pass.add_argument("--kind", choices=("daily", "intraday"), required=True)
    record_pass.add_argument("--outcome", choices=("completed", "offline", "lock_skipped"),
                             required=True)
    record_pass.add_argument("--started", default=None,
                             help="UTC time the batch started, as the batch file prints it")
    record_pass.add_argument("--marker", default=None,
                             help="the pass marker the batch file has just written")
    record_pass.set_defaults(func=cmd_record_pass)

    collect = sub.add_parser(
        "collect",
        help="collect a source list into the database",
        description="Run every enabled discovery method for each configured source "
                    "and store what it finds. Each source reports ok, zero yield, "
                    "failed, or paused (every method off, nothing sent) and resumes "
                    "from its own watermark; a failed source "
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
    collect.add_argument("--exclude", action="append", default=[], metavar="SLUG",
                         help="treat this source as paused for this pass: nothing is sent, "
                              "its watermark holds. Repeatable or comma-separated")
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
    bodies.add_argument("--title-gate-client", default=None,
                        help="fetch only title-only URLs kept in this client's JSONL decisions")
    bodies.add_argument("--exclude", action="append", default=[], metavar="SLUG",
                        help="leave this source's queue untouched for this pass. "
                             "Repeatable or comma-separated")
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

    alert_gate = sub.add_parser(
        "alert-gate",
        help="email one digest of potential alerts in newly admitted news",
        description="After the news relevance gates have run, inspect newly admitted "
                    "news bodies carrying an own-brand or category 4/5 alert term. "
                    "A small model makes a binary potential-alert decision and writes "
                    "a short Chinese summary. All positives are sent in one email "
                    "directly from this host. Needs OPENAI_API_KEY, EMAIL_SENDER, "
                    "ALERT_EMAIL_RECIPIENT (or EMAIL_RECIPIENT), and SMTP_PASSWORD "
                    "in the laptop's .env when matching items exist.",
    )
    alert_gate.add_argument("--client", default="jt-express",
                            help="client profile slug or path (default: jt-express)")
    alert_gate.add_argument("--dry-run", action="store_true",
                            help="show newly eligible term matches without model calls, "
                                 "state changes, or email")
    alert_gate.set_defaults(func=cmd_alert_gate)

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

    dip = sub.add_parser(
        "collect-dip",
        help="collect Bundestag and Bundesrat procedures from DIP",
        description="Store every Bundestag and Bundesrat procedure whose DIP record "
                    "changed since the watermark - bills, resolutions, parliamentary "
                    "questions and their answers - except the procedural types the "
                    "source entry excludes. One item per procedure; a new step writes "
                    "a new version. Uses the Bundestag's public API key unless "
                    "DIP_API_KEY is set in .env.",
    )
    dip.add_argument("--days", type=float, default=None,
                     help="window in days; default resumes from the watermark")
    dip.add_argument("--lookback", type=positive_int, default=30,
                     help="days to collect on a first run with no watermark (default: 30)")
    dip.add_argument("--verbose", action="store_true")
    dip.set_defaults(func=cmd_collect_dip)

    ep = sub.add_parser(
        "collect-ep",
        help="collect European Parliament legislative procedures",
        description="Fetch every legislative procedure the Parliament lists in scope - "
                    "COD, CNS and APP since the source entry's since_year - and store it "
                    "when its events, stage or scheduled activities changed. A procedure "
                    "with recent or scheduled activity is polled every run, a dormant one "
                    "weekly, and one published in the Official Journal never again. The "
                    "API rate-limits without warning, so a run that keeps being refused "
                    "stops and leaves the rest for the next one. No credential is required.",
    )
    ep.add_argument("--since-year", type=positive_int, default=None,
                    help="earliest procedure year (default: the source entry's since_year)")
    ep.add_argument("--sweep", action="store_true",
                    help="poll dormant procedures too, whatever the weekly schedule says")
    ep.add_argument("--verbose", action="store_true")
    ep.set_defaults(func=cmd_collect_ep)

    dip_docs = sub.add_parser(
        "fetch-dip-docs",
        help="fetch the documents behind DIP procedures the body gate kept",
        description="For every DIP procedure the regulatory body gate judged relevant for "
                    "the client, fetch the Drucksachen its steps reference - the answer, the "
                    "bill, the committee report - and store their text. Plenary protocols "
                    "and list entries are skipped. A document DIP has no text for yet stays "
                    "pending and is asked for again on the next run.",
    )
    dip_docs.add_argument("--client", default="jt-express",
                          help="client profile slug or path (default: jt-express)")
    dip_docs.add_argument("--limit", type=positive_int, default=None,
                          help="maximum documents per run, newest first (default: 50)")
    dip_docs.add_argument("--retry-unavailable", action="store_true",
                          help="also retry documents given up on")
    dip_docs.add_argument("--show", metavar="PROCEDURE_ID", default=None,
                          help="print what the full assessment would read for one procedure - "
                               "its record and cut documents - and fetch nothing")
    dip_docs.set_defaults(func=cmd_fetch_dip_docs)

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

    sg_view = sub.add_parser(
        "safety-gate-view",
        help="print the client's Safety Gate view without calling a model",
        description="Apply the client's geography and key-customer rules to the stored "
                    "Safety Gate alerts and print what the full assessment would read. "
                    "Read-only: it writes no assessment rows and makes no model or "
                    "network calls. Relevance here is deterministic - two fields decide "
                    "it - so these alerts skip the selector and the body gate.",
    )
    sg_view.add_argument("--client", default="jt-express",
                         help="client profile slug or path (default: jt-express)")
    sg_view.add_argument("--since", type=iso_date, default=None,
                         help="only alerts published on or after this date")
    sg_view.add_argument("--key-customers", action="store_true",
                         help="only alerts whose online trader is a client customer")
    sg_view.add_argument("--full", action="store_true",
                         help="print each composed body instead of one line per alert")
    sg_view.set_defaults(func=cmd_safety_gate_view)

    export = sub.add_parser(
        "export-window",
        help="freeze one client-week of stored material into a report bundle",
        description="Read-only export of everything a weekly report may use: the "
                    "candidates, the items each gate stopped, the full gate census, "
                    "per-source coverage, and a manifest with the window, the counts, "
                    "the stated policy and a hash of every input that shaped the "
                    "selection. Opens the database through a mode=ro URI inside a "
                    "rolled-back transaction, so it is safe while collection runs and "
                    "cannot alter what it reports on. Writes no database rows.",
    )
    export.add_argument("--client", default="jt-express",
                        help="client profile slug (default: jt-express)")
    export.add_argument("--since", type=iso_date, required=True,
                        help="first day of the window, local time (YYYY-MM-DD)")
    export.add_argument("--until", type=iso_date, required=True,
                        help="last day of the window, inclusive (YYYY-MM-DD)")
    export.add_argument("--out", type=Path, default=None,
                        help="bundle directory (default: data/reports/<client>-<since>_<until>)")
    export.set_defaults(func=cmd_export_window)

    def bundle_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--bundle", type=Path, default=None,
                            help="bundle directory from export-window")
        parser.add_argument("--client", default="jt-express",
                            help="with --since/--until, names the bundle instead")
        parser.add_argument("--since", type=iso_date, default=None)
        parser.add_argument("--until", type=iso_date, default=None)

    assess = sub.add_parser(
        "assess",
        help="assess a frozen bundle: carry-forward, triage, cluster, read, challenge, write",
        description="The only stage that calls a model. Six steps over a frozen "
                    "bundle, with state on disk rather than in a preserved "
                    "conversation: last cycle's open issues are searched for (in the "
                    "gate's rejects too), candidates are triaged and clustered into "
                    "stories, each story is read with tools, challenged adversarially, "
                    "and the Chinese report is drafted from the resulting issue "
                    "register. Writes decisions.json, issue-register.json, "
                    "report-draft.json and the tool-call log into the bundle; touches "
                    "no database row and makes no source network call.",
    )
    bundle_args(assess)
    assess.add_argument("--model", default=None,
                        help="override the model pinned in config.json")
    assess.add_argument("--max-stories", type=positive_int, default=None,
                        help="cap on deep reads for this run")
    assess.add_argument("--no-write", action="store_true",
                        help="stop after the register; skip drafting the report")
    assess.set_defaults(func=cmd_assess)

    report = sub.add_parser(
        "report",
        help="render the Chinese report, ledger and coverage from an assessed bundle",
        description="Deterministic. Joins the assessor's decisions against the frozen "
                    "export and renders four local pages. Every source link in the "
                    "report resolves through the frozen evidence by id, so a URL the "
                    "export does not contain fails the build rather than reaching a "
                    "customer. Re-runnable on the same bundle.",
    )
    bundle_args(report)
    report.add_argument("--record", action="store_true",
                        help="also file a row in the report table")
    report.set_defaults(func=cmd_report)

    verify = sub.add_parser(
        "verify-report",
        help="check a rendered bundle's accounting, dates, links and citations",
        description="Deterministic checks that know nothing about the model, with "
                    "every constant read from the manifest: one decision per identity "
                    "and no gaps, nothing fetched after the cutoff, the census "
                    "reconciling, no lastmod date presented as a publication date, "
                    "every report URL present in the frozen evidence, and no broken "
                    "local link, replacement character or unrendered markdown. Writes "
                    "verification.json and bundle-hashes.json; exits 1 on any error.",
    )
    bundle_args(verify)
    verify.set_defaults(func=cmd_verify_report)

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
