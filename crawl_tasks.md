# Crawl stabilisation — remaining launch work

Updated **2026-09-16**, for go-live in about two weeks. This replaces the old
T1–T10 implementation briefs: completed work is removed. Findings and supporting
evidence are in [weaknesses.md](weaknesses.md); this file is the execution order
and the monitoring plan. Recommendations below have not been implemented by this
audit.

## Current position

- Reviewed checkout: `637603f`, plus the existing, uncommitted WELT `/deals`
  exclusion. Both the production laptop and the health VPS still run `9cb3ea4`.
  Polite HTTP and the earlier T2–T9 changes are deployed; pinned discovery,
  ZEIT's discovery pause, and the new outage handling are not deployed there yet.
- All **17 enabled news sitemap sources are pinned** in the reviewed config.
  The recorded September 16 probe measured **80 discovery requests / 35.6 MB**
  cold, versus 471 / 180 MB for the previous warm traversal. This is a probe
  measurement, not a sustained production measurement or a whole-pipeline budget.
- The laptop's daily task is enabled. **Intraday is disabled**, verified from
  Task Scheduler settings, even though `tools/laptop.py status` prints a next
  trigger. Do not assume nine passes/day or two-hour recovery while it is paused.
- Canaries are disabled in the new checkout; the deployed September 16 daily run
  still ran them. The remaining local coverage observer learns from stored yield;
  `rediscover` is a manual comparison using the production parser.
- The September 16 daily marker is failed (`worst_exit=2`). The VPS timer is
  active and its state records a `run_failed` notification at 04:15:12 UTC.
  Its notification latch can mean email **or** push delivered; mailbox/phone
  receipt was not checked here.
- **366 focused existing tests passed**, covering discovery, polite HTTP,
  watermarks, bodies, gates, structured collectors, health and rediscovery.
  Additional offline fault cases exposed W18–W25. Tests do not establish live
  coverage. Production inspection was read-only; no crawl, deployment, recovery,
  paid model call or notification was initiated.

## Before the final validation week

### C1. Deploy the intended configuration and reconcile recovery

**Priority: first operational step.** The source fixes have already restored
new September 15 arrivals from DVZ, VerkehrsRundschau, Händlerbund and etailment.
That does not establish that the earlier gap was recovered completely. BVDW's
latest first-version row is still September 10; its old canary lists four missing
URLs, which need checking against current exclusions before calling them losses.

Deploy a reviewed commit to the laptop and VPS, recording the actual commit and
source-config hash. Confirm that the intended ZEIT pause, canary switch and
sitemap pins reached production. Keep intraday's restart a deliberate operational
step after C2–C4; a code pull does not enable that task.

Reconcile the September 10–16 affected window source by source. Separate missing
articles, renamed URLs, deliberate exclusions, and entries no longer recoverable
from a publisher's current feed/news sitemap. Recover only with an actual held
run lock; checking that the lock was free earlier does not acquire it. The old
blanket `collect --days 7` recipe is withdrawn: C4 explains why it cannot prove
recovery. Replay affected title-gate run ids through the existing repair workflow.

**Done when:** both hosts' revisions/settings are recorded, the first production
pass has per-source outcomes and traffic counts, and every sampled gap has a
recorded explanation or recovered identity. No unresolved loss is marked fixed
because the daily task returned zero.

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

### C3. Make a publisher pause stop all requests (W20)

**Priority: before resuming intraday / contacting ZEIT again.** Files:
`src/collect.py`, `src/bodies.py`, `src/polite_http.py`, health source accounting.

A disabled source still reaches the origin probe; the existing body queue also
bypasses the disabled-source check applied to new title-gate keeps. Fix those
paths together. Keep queued work recoverable and display the source as paused,
not as a successful quiet source.

Extend the publisher cooldown rule to body requests and future passes. Discovery's
adapter currently stops only its own pass; body fetching neither shares that
state nor honours `Retry-After`. A long server-requested delay must survive the
next scheduled run. A temporary host block must not retire all its articles as
permanently unavailable after five attempts.

**Done when:** a disabled source with old queued keeps sends zero origin,
discovery and body requests; a throttled publisher remains deferred until its
retry time; unrelated sources continue. Re-enabling resumes retained work.

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

### C6. Close the remaining structured-collector failure paths (W24, W25)

**Priority: before launch.** Files: `src/ep_procedures.py`, `src/safety_gate.py`,
`run.py`, `health/analyze.py`, their existing tests.

- EP detail throttling must appear in source status and the CLI result. Currently
  three rate-limit refusals stop a run with no fetched procedures but record
  `status=ok` and exit 0. Failed/skipped dormant procedures must remain due; a
  partial sweep must not postpone them for another week.
- Safety Gate must validate the expected report structure before checkpointing:
  a well-formed `<error>…</error>` response currently counts as an empty successful
  report and advances its watermark. Preserve legitimate empty-report handling
  only when the expected structure is present.
- Record Safety Gate's successful no-new-report checks and failed attempts, and
  include it in health accounting. T3 added DIP/EP health rows, but Safety Gate
  has no configured collector entry. Its weekly publication cadence needs a
  last-successful-check signal, not a 48-hour last-new-report alarm.

**Done when:** throttled EP detail fetches and malformed Safety Gate response
shapes cannot look healthy or skip work; a normal quiet weekly source does not
raise an absence alarm. Prove recovery on the next attempt with offline fixtures.

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
The coverage observer runs only daily, so a partial intraday source failure can
remain absent from VPS alerts until the next morning even while the timer polls.

| Signal | Where / comparison | Action threshold during validation |
|---|---|---|
| Actual schedule and revision | Task **Enabled/State**, last/next start, deployed commit/config hash | Any missed intended slot, unexpected revision, repeated lock skip, or runtime approaching the 90-minute intraday / 4-hour daily limit |
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
