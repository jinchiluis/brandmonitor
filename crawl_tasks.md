# Crawl stabilisation — remaining launch work

Updated **2026-09-16**, for go-live in about two weeks. Findings and supporting
evidence are in [weaknesses.md](weaknesses.md); this file is the execution order
and the monitoring plan. Finished work is checked off under **Done** with one line
each; its brief is removed.

## Current position

- Production runs the current main branch. Every pass records its commit and
  config hashes in `pipeline_pass` and per-source discovery traffic in
  `discovery_source`.
- All **17 enabled news sitemap sources are pinned**. The first pinned production
  pass (2026-09-16 afternoon) sent **65 discovery requests / 27.1 MB** across 22
  news sources, against 471 / 180 MB for the old warm traversal. One pass, not a
  sustained measurement.
- The daily task is enabled. **Intraday is disabled**, verified from Task
  Scheduler settings, even though `tools/laptop.py status` prints a next trigger.
  Do not assume nine passes/day or two-hour recovery while it is paused, and
  restart it deliberately after C2–C4; a code pull does not enable it.
- Canaries are disabled. The coverage observer learns from stored yield;
  `rediscover` is a manual comparison using the production parser.
- The 06:00 run on September 16 failed (`worst_exit=2`) in a DNS outage; the
  afternoon re-run finished with every stage at 0. The VPS recorded a
  `run_failed` notification; its latch means email **or** push delivered.

## Done

- [x] **C1 deployment** — pins, ZEIT pause, canary switch and outage handling are
  live, with commit/config hash and per-source traffic recorded per pass. The
  reconciliation half of C1 is still open below.
- [x] **C3 pause** — a paused source sends no origin, discovery or body request,
  keeps its queued bodies and holds its watermark (tested). Throttling is still
  open below.
- [x] **C6 structured collectors (W24, W25)** — EP throttling records `failed` and
  holds the weekly sweep; Safety Gate rejects a document without the
  `Safety-Gate` root/`report_date` and writes a run on every check; the health
  analyzer judges Safety Gate. After the next deploy, check that
  `python tools/laptop.py sql "SELECT id, status, note FROM run WHERE
  kind='safety_gate' ORDER BY id DESC LIMIT 3"` shows a run from the latest daily
  pass, then remove W24/W25.

## Before the final validation week

### C1. Reconcile the September 10–16 gap

**Priority: first operational step.** The source fixes have already restored
new September 15 arrivals from DVZ, VerkehrsRundschau, Händlerbund and etailment.
That does not establish that the earlier gap was recovered completely. BVDW's
latest first-version row is still September 10; its old canary lists four missing
URLs, which need checking against current exclusions before calling them losses.

Reconcile the September 10–16 affected window source by source. Separate missing
articles, renamed URLs, deliberate exclusions, and entries no longer recoverable
from a publisher's current feed/news sitemap. Recover only with an actual held
run lock; checking that the lock was free earlier does not acquire it. The old
blanket `collect --days 7` recipe is withdrawn: C4 explains why it cannot prove
recovery. Replay affected title-gate run ids through the existing repair workflow.

**Done when:** every sampled gap has a recorded explanation or recovered identity.
No unresolved loss is marked fixed because the daily task returned zero.

### C2. Propagate failed and incomplete discovery (W18, W19)

**Priority: before launch.** Files: `vendor/newscrawler/crawler.py`,
`src/discovery.py`, `src/collect.py`, discovery/watermark tests.

- A configured feed or frontpage section that times out, returns an error or serves
  a challenge page must produce an incomplete/failed source outcome. Preserve
  successfully collected hints, but do not advance over unread required work.
  A readable empty feed remains a valid quiet result. Guessed optional paths are
  different from explicitly configured endpoints.
- Strict pinned reads must distinguish complete XML from a recovered fragment,
  and an expected article file from a newly returned sitemap index. Reject or
  explicitly handle the latter; silently ignoring its children is not success.
- Restrict grouped 404/410 tolerance to verified optional pages/files. Reading
  September must not excuse a missing required August file in a recovery window.
- Surface frontpage truncation and apply the budget to eligible article URLs;
  300 excluded links currently crowd out the 301st real article without a warning.

**Done when:** offline timeout, 403/500, HTML, partial XML, leaf-to-index,
required-file-404 and frontpage-cap cases cannot report a complete source or
advance its watermark. Valid empty and 304 replay cases still pass. Successful
sibling results remain available, with the incomplete interval retried.

### C3. Make body fetching respect publisher throttling (W20)

**Implemented 2026-09-17; rollout pending.** `rejection_plan.md` records the agreed
23-hour cooldown, 403/429/503 attempt protection, seven-day history and intraday
health alarm. Deploy the matching VPS checker and verify the alarm before
re-enabling intraday. The schedule has not been changed by this implementation.

**Priority: before resuming intraday / contacting ZEIT again.** Files:
`src/bodies.py`, `src/polite_http.py`, health source accounting.

Collection and body fetching now share a persistent source cooldown and honour
longer `Retry-After` delays across stages and passes. A temporary 403/429/503 block
does not spend body attempts or retire the queue; unrelated sources continue.

**Done when:** a throttled publisher remains deferred until its retry time across
stages and passes; unrelated sources continue; a temporary host block does not
retire its queued articles as unavailable.

### C4. Establish how far each source can recover (W21)

**Priority: before declaring outage recovery automatic.** Files:
`src/discovery.py`, source config, `tools/rediscover.py`, source-coverage runbook.

Record each source's listing retention and recovery route. Current pins are
measured for roughly three days, not the nominal 30-day emergency lookback:
Spiegel reads pages 1–9 regardless of the requested duration; `{LATEST}` selects
only the configured newest chunks; a news sitemap/feed cannot return articles
it has already removed. A wider date filter does not widen these inventories.

Keep routine collection small. Use bounded additional pages/files or a separate
manual archive recovery route for longer gaps. Detect when the oldest material
read does not cover the required interval instead of claiming completion.

**Done when:** normal two-hour and daily operation, a three-day outage, a seven-day
recovery, newest-page rollover, and **September 30 → October 1** are reconciled.
Exercise absent current-month files and missing required prior-month files
separately. Record irrecoverable feed/frontpage gaps explicitly. Use fixtures for
boundary/failure cases and budget any live recovery measurement per source.

### C5. Restore a trustworthy independent coverage check (W16, W22)

**Priority: throughout the remaining two weeks.** Files: `health/canary.py`,
`health/canaries.json`, `health/analyze.py`, `tools/rediscover.py`.

Keep the canary pause's traffic rationale. Before re-enabling, give it its own
conditional cache, request/byte budget, explicit User-Agent and throttle outcome,
plus current exclusions and a publication/discovery grace period. Compare with an
independent section/feed where possible: a second parser reading the same narrowed
sitemap cannot see the section the publisher stopped listing there.

Until then, assign a daily manual comparison for priority sources. Monthly
rediscovery alone is too slow when a short-lived listing may disappear before
anyone checks. Sample DVZ, VerkehrsRundschau, Händlerbund, etailment, OHN, SZ,
feed-only sources and the regulatory feeds on a rotation that covers each
priority source within its retention window.

`rediscover` must report an **inconclusive** walk when a cap, failed file or
throttle prevents a full comparison. It currently omits the traversal's cap
report and can print no missing URLs after examining only 100 of 101 files.
It remains a useful drift diagnostic, not an independent parser or proof that
the discovered URLs were stored and gated.

**Done when:** a deliberately omitted article/section reaches the operator within
the agreed review interval; a capped/broken comparison cannot claim complete
coverage; each sampled omission is explained, including at the old 70% canary
threshold. Verify the first complete weekday/weekend cycle with the actual
schedule. There is no fixed September 19 baseline-readiness date anymore.

### C7. Validate handoffs and recovery, then hold a stable configuration (W23)

**Priority: before customer delivery.** Files: existing title-gate repair workflow,
`src/title_gate.py`, `src/body_gate.py`, `src/alert_gate.py`, health/runbooks.

The title gate normally processes only the latest news run's first-seen items.
A crash or fatal model configuration error can leave earlier items permanently
unjudged. The manual repair command exists; prove it and keep the affected run id
open until repaired. A clean next-day marker is not evidence of repair. Decide
whether launch uses a durable automatic backlog or an explicitly owned manual
repair check; there must be an accountable path either way.

Also decide what happens when a previously dropped title/body becomes relevant
through a material update. Identity-wide decisions currently prevent ordinary
re-gating. This is separate from the fixed discovery-version churn.

Perform a restore into a disposable location using a matching database snapshot
and state archive obtained from off-host storage. Check SQLite integrity,
watermarks, retained title keeps and frozen report/register references. A copied
OneDrive directory and `backup=0` do not establish cloud upload or restoration.

**Done when:** an interrupted handoff is reconciled, relevant unavailable bodies
have explicit review outcomes, the restored copy passes checks, and the intended
production schedule/configuration has **seven consecutive observed days**,
including a weekend. A material change restarts validation of its affected path.

## What to monitor closely

These are proposed launch-period triggers, not already-implemented alarms.
Check after the daily run; when intraday resumes, inspect its source outcomes too.
The coverage observer now runs after intraday too. Partial source failures may
still exit zero, so inspect the source findings rather than only the stage code.

| Signal | Where / comparison | Action threshold during validation |
|---|---|---|
| Actual schedule and revision | Task **Enabled/State**, last/next start, `pipeline_pass` commit/config hash | Any missed intended slot, unexpected revision, repeated lock skip, or runtime approaching the 90-minute intraday / 4-hour daily limit |
| Per-source completion and watermarks | `run`, `run_source`, collection watermark | Any required-file failure, held/incorrectly advanced watermark, simultaneous unexplained zeros, or truncated/inconclusive pass; do not rely on the aggregate exit code |
| Independent article coverage | Publisher sample → stored identity → gate/body outcome | Every unexplained relevant omission after grace; 7/10 can be healthy under the old canary and still miss three articles |
| Traffic and publisher response | Per-source discovery request/MB/304/replay logs plus body/browser activity | Investigate >120 discovery requests or >40 MB on a comparable normal pinned pass, repeated 403/429/503, falling validator reuse, or traffic from a paused host; recovery passes need separate budgets |
| Recovery headroom | Oldest required time versus oldest listed article/page, source retention | Investigate before the backlog approaches a feed/news-sitemap retention limit; monthly boundary and chunk rollover need special attention |
| Unfinished and retired bodies | Queue age/count by source; failed → unavailable transitions and reasons | Any selected article waiting >24 h as an initial review threshold, sustained growth, or any technical failure retired at the attempt cap; a shrinking retry queue alone is not recovery |
| Gates and alerts | Collection run ids versus eligible title decisions, body decisions, alert decisions and `sent_at` | Unjudged eligible items after a successful pass, persistent model errors/fail-open decisions, relevant unavailable bodies without disposition, unsent positives |
| Content quality and dates | Small sample of successful bodies, source exclusions/hub rejects, date provenance, logical publisher ids | New template/teaser extraction, relevant undated rejects, duplicate alerts from renamed URLs, archive stories shown as new; measure findings per identity rather than version count |
| Health delivery and backup recovery | Fresh observer JSON, VPS condition/latch, received channel; off-host snapshot + state archive | Missing/stale observations, unresolved incidents disappearing without reconciliation, delivery failure, missing off-host files or an unproven restore |

For `{LATEST}` sources, also watch repeated index downloads: `resolve_latest`
fetches the index but does not save it to `DiscoveryCache`, so its conditional
cache benefit is currently absent. This is a small traffic follow-up, not a reason
to restore full-tree traversal.

## Keep out of the crawl stabilisation batch

Keep W9/W10 date and duplicate-identity decisions visible to the report reviewer;
keep W1/W15 undated/cumulative-export work in the report backlog. Do not combine a
historical identity migration, broad source pruning, new paid subscriptions or
report-agent redesign with these crawl fixes. Source value needs several healthy
weekly cycles, not the outage-contaminated first week. W17's customer alert-scope
question remains open.
