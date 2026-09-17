# Open weaknesses and launch evidence

Reviewed **2026-09-16** against the code, production read-only SQL/logs/health
snapshots, Task Scheduler settings and the VPS notification state. Re-checked the
same evening: production runs the current main branch, and the discovery,
rediscovery, canary and gate files that W18, W19, W21–W23 cite have not changed.

The active action plan and monitoring thresholds are in
[crawl_tasks.md](crawl_tasks.md). Completed fixes and their old implementation
briefs have been removed; Git retains that history. Existing unresolved finding
numbers are retained. W18–W25 below are now explicit findings re-derived from the
current code, not the missing entries cited by the previous audit table.

Evidence labels matter: **production** means observed stored state;
**reproduced** means an offline mocked fault or disposable test database;
**code review** means a reachable path, not a measured publisher incident.
The fault cases below demonstrate behaviour the test suite does not currently
reject.

## Recovery is still open

**Production** runs the pinned sitemaps, the outage preflight and body transport
protection, and the paused-source handling. Every pass records its commit and
config hashes in `pipeline_pass`. The daily task is enabled; the intraday task's
`Settings.Enabled` is **false**. A printed next trigger does not mean it will run.
Canaries are disabled.

The 06:00 Berlin run on September 16 ended at 04:10:57 UTC with
`worst_exit=2`: title bodies 1, Safety Gate 2, DIP 1, EP 2. News run 143 recorded
23 zero sources and SZ as its only nonzero source; regulatory run 146 recorded
all eight crawled sources as zero with no source error. DIP/EP errors identify DNS
failure. The manual re-run that afternoon finished with every stage at 0. The
network preflight helps an outage already present at startup; it cannot prove
every publisher is reachable or catch a later outage.

DVZ, VerkehrsRundschau, Händlerbund and etailment have new first-version rows on
September 15, so the old statement that they remain completely stuck is obsolete.
BVDW's latest new identity is September 10. Historical completeness remains
unreconciled. The last canary snapshot has DVZ, VerkehrsRundschau and t3n at
**7/10**, which meets its 70% healthy threshold, and BVDW at **6/10**. Missing URLs
need checking against exclusions, aliases and discovery grace; these counts alone
are not proof of either loss or completeness.

The VPS timer is active; its state at 09:00:25 UTC records `run_failed`, notified
at 04:15:12 UTC. The checker latches if either email or push delivers, so this
proves a recorded notification outcome, not receipt in both channels. Production
also has two positive alert decisions with `sent_at` populated. Old backlog items
claiming that no alert had ever been sent are stale.

The body queue has 2,940 successful, three retryable and 60 unavailable rows, with
no pending rows. No latest news row lacks a body that an earlier version already
held, so the old W2 hidden-body repair is no longer outstanding. Queue drainage
does not establish that unavailable articles or pre-gate exclusions were acceptable.

**Next:** C1 reconciliation and C7 recovery evidence. A code fix does not recover
a listing that has already disappeared.

## W18. Feed/frontpage failures and frontpage caps can still look complete

**Priority:** before launch. **Evidence:** production context above, plus reproduced
faults. Files: `vendor/newscrawler/crawler.py` (`collect_from_feeds`,
`collect_from_frontpage`), `src/collect.py` (`collect_source`, `run_collection`).

An explicitly configured feed answering **403, 404, 500 or HTTP 200 HTML** returns
no hints and no error. A frontpage fetch returning no document does the same.
Both collectors also catch individual exceptions internally, so the outer
`collect_source` exception handling does not see those failures. `run_collection`
then records `zero` and advances that source's watermark. A successfully read
sibling endpoint can hide a partial failure too. The 429/503 discovery breaker
catches its own throttle conditions; it does not fix these other cases.

This still affects the feed-only news sources and most regulatory article
sources after pinning. The September 16 deployed zero results during a DNS outage
show why an empty result cannot be treated as transport success. The 48-hour overlap
provides some recovery slack, not a guarantee after prolonged failures or short
feed retention.

Frontpage discovery also slices the first **300 raw links** before source
exclusions/furniture filtering, without a truncation outcome. Reproduced: 300
excluded links followed by one valid article produces zero retained hints, no
error. The previously fixed sitemap cap reporting does not cover this path.

**Needed:** C2. Carry required-endpoint failures and truncation into source status;
hold incomplete checkpoints, retain successfully retrieved material, and keep
well-formed empty results valid. Apply article budgets after eligibility filtering.
Do not turn every failed optional guessed path into an outage.

## W19. Strict pins still accept incomplete data or a changed file role

**Priority:** before launch. **Evidence:** reproduced. Files:
`vendor/newscrawler/crawler.py:fetch_sitemap_urls`,
`src/discovery.py:collect_from_pinned`.

Three separate cases currently return success:

1. A truncated sitemap missing its closing `</urlset>` is repaired by lxml's
   recovery parser even with `strict=True`. The surviving entries are accepted.
   A truncated response can therefore move the watermark over entries it lost.
2. A pinned article file returning a valid `<sitemapindex>` passes the root check,
   but `collect_from_pinned` ignores its children. Reproduced with one child
   containing an article: zero hints, no error, child never requested.
3. A multi-month pin with a required August file answering 404 and September
   answering 200 succeeds. Any grouped 404/410 is excused if a sibling reads;
   it is not restricted to a not-yet-created current-month/page file.

Transport errors, HTML, off-host redirects, unparseable roots and grouped 5xx
already have protections; do not rewrite those as unfinished. The gaps are the
complete-document/expected-role contract and which absences are genuinely optional.
Undated pinned entries are also deliberately skipped with an INFO message; monitor
that count for date-schema changes instead of assuming every readable XML file
contributes eligible articles.

**Needed:** C2. Preserve valid-empty/304 behaviour, require a complete usable
article inventory, and test required versus optional missing files across the
month boundary. Do not silently restore unrestricted recursive discovery.

## W20. Body requests ignore publisher throttling

**Implementation update, 2026-09-17:** the agreed fix is in `rejection_plan.md`:
23-hour source cooldowns (longer `Retry-After` wins), no spent attempts for body
403/429/503, and coverage analysis after daily and intraday passes. Matching VPS
checker deployment and alarm verification remain before enabling intraday.
The evidence below describes the behavior before this fix.

**Priority:** before resuming intraday or contacting ZEIT again.
**Evidence:** code review and production. Files: `src/bodies.py`,
`src/polite_http.py`.

A paused source (every discovery method off) now sends no origin, discovery or
body request, keeps its queue and holds its watermark (`test_discovery.py`,
`test_bodies.py`). What remains is throttling:

- `PoliteAdapter` belongs to one discovery pass. `_fetch_public` uses plain
  `requests.get`; browser/subscriber fetching is also outside that adapter.
  Body requests do not share discovery's throttle state or honour its
  `Retry-After`, and the next discovery pass has forgotten a long cooldown.
- Body HTTP/timeout failures count toward five attempts and then become
  `unavailable`. The new network protection covers connection failures **before
  the first successful body in that pass**. It does not cover throttles or a
  network outage starting later in the pass.

**Production:** three ZEIT bodies were still retryable at three attempts on
September 16 (403/read timeout). One Verbraucherzentrale item had already become
unavailable after five 403 attempts. This establishes the terminal-error path;
it does not prove that particular page will become fetchable later.

**Needed:** C3. Respect host cooldowns across stages/passes and distinguish
publisher access trouble from permanent article unavailability. Avoid an automatic
bulk retry of unavailable bodies against a blocked publisher.

## W21. A wider date window does not widen the pinned inventory

**Priority:** recovery contract before launch. **Evidence:** code review and the
recorded September 16 measurements in [source_coverage.md](docs/source_coverage.md).
Files: `src/discovery.py`, source `sitemap_urls`/`feed_urls`.

The source window widens from a held watermark and overlaps by 48 hours. However:

- Spiegel still requests **pages 1–9** of each named month for a seven- or
  thirty-day recovery. Nine pages hold at most 450 listed entries per month;
  no boundary check proves they reach the beginning of the requested interval.
  The previous 3-day comparison already required widening the original range.
- `{LATEST}` selects the configured highest-numbered chunks irrespective of the
  window. When a publisher starts a new chunk, the previous chunk may still carry
  unseen articles inside that window. With one chunk selected it is no longer read.
- A news sitemap, finite feed or frontpage may no longer list articles missed
  during an outage. DVZ's recent-news pin is intentionally small; a seven-day
  filter cannot turn it into an archive.

These are inventory limits, so all requested files can be readable and the source
can still be incomplete. Removing the old crawler's caps did not remove these
limits. Daily-only operation currently makes the normal date span roughly
**72 hours** (24 hours plus overlap), leaving less headroom than the intraday case.

**Needed:** C4. Record retention and recovery routes per source, detect a requested
window beyond that coverage, and reconcile outage/page/month rollover cases.
Preserve cheap routine collection; do not restore full-tree walks nine times daily.

## W16 / W22. Independent detection is paused, and rediscovery can overstate certainty

**Priority:** launch monitoring gap. **Evidence:** config, code review, production
snapshot and reproduced cap case. Files: `health/analyze.py`, `health/canaries.json`,
`tools/rediscover.py`.

The ten canaries exist but are disabled (`enabled: false`). The analyzer
deliberately stops expecting their snapshot.
The measured reason for the pause—unconditional traffic and configuration churn—
remains valid; it also removes the only automated independent publisher comparison.

Weekend exclusion is implemented and tested. Do not keep the old T2 task or its
September 19 readiness deadline. The September 16 production snapshot still has
32 article sources learning, with three comparable prior days for Spiegel. Yield
rules need seven comparable **weekdays** and a median of at least two stored rows.
A source that is broken during learning, or naturally low-volume, can stay below
that floor. Counts measure stored versions, not solely new article identities;
retitles/enrichment can also make a source look busy.

`rediscover` is useful but has three limits:

- It uses the production sitemap parser and filters on both sides, so common
  parser/filter errors can agree perfectly. It does not compare against SQLite
  or gate outcomes. A second parser on the same narrowed publisher file also
  cannot prove coverage of omitted sections.
- It does not pass a `report` dictionary to the full walker. Reproduced with 101
  files and a distinct article only in file 101: **100 files read, missing `{}`,
  error `None`**. The full-walk comparison is actually incomplete. Swallowed file
  failures can similarly reduce its reference set.
- Monthly checks can detect drift after short-lived missed listings are gone.

Current health also has operational blind spots: partial intraday source failure
may still exit 0, and the optional intraday marker has no freshness alarm. The
coverage analyzer now runs after both daily and intraday passes (C3).
`_body_metrics` warns on retryable rows
with at least three attempts, but that warning disappears when they retire to
`unavailable`; there is no queue-age/retirement incident.

**Needed:** C5 and the monitoring table. Until a budgeted independent check is
restored, explicitly own manual comparisons. A healthy threshold is a triage rule,
not permission to ignore its missing-URL list. Keep unresolved losses open until
reconciled, even if the following marker is green.

## W23. Collection-to-gate recovery and changed evidence still need an explicit contract

**Priority:** prove the handoff before launch; decide update policy.
**Evidence:** code review, documented existing behaviour; no new production loss
attributed to this path in this audit. Files: `run.py:cmd_gate`,
`src/selector.py:selection_from_db`, `src/body_gate.py:pending_items`,
`src/title_gate.py`, `src/alert_gate.py`.

The routine title gate reads the latest news run and filters by an identity's
first collection run. A crash/fatal gate error after collection can strand its
items once a newer collection run takes over. `tools/repair_title_gate.bat`
already repairs named run ids; automatic old-run drainage is not implemented.
Transient unusable replies that retain an entire batch are a different, handled
case. The repaired alert gate's per-item failure handling does not fix a fatal
error in the earlier title/body gates.

Separately, a changed title can now be stored correctly without date-only churn,
but the normal title gate still considers only first-seen identities. A previously
irrelevant title becoming relevant is not automatically reconsidered. The body
gate excludes identities with **any** previous decision for that tier, including
when a new body/procedure version arrives. A previously unsure untitled EP
procedure can stay stopped after gaining its title. Decide what constitutes a
material update and how it re-enters selection without repeat alerts.

**Needed:** C7. Prove interrupted-run repair, track unjudged eligible work until
accounted for, and make the manual/automatic ownership explicit. Audit changed
rejected identities during the pilot; do not equate raw version storage with a
new relevance decision. Paywalled title-gate fallback is already implemented;
monitor its decisions rather than reimplementing T6.

## W24. EP detail throttling can still finish healthy

**Priority:** before launch. **Evidence:** reproduced. Files:
`src/ep_procedures.py:run_ep_collection`, `_finish`, `run.py:cmd_collect_ep`.

Unknown listed procedures are now a separate benign outcome, and failed listings
are visible. The remaining case is **detail throttling**: `EpRateLimited` increments
a refusal counter and can set `stopped`, but adds nothing to `errors`. `_finish`
and the CLI use `errors`, not `stopped`, to decide failure. Three mocked detail
429s produced `fetched=0`, `stopped=rate limited…`, `errors=[]`, stored run
`status=ok`, and CLI exit expression 0. The health observer therefore misses it.

Also, a sweep advances its sweep watermark whenever `stopped is None`, even when
individual details failed or isolated rate limits were skipped. An already-stored
dormant procedure can wait until the next weekly sweep instead of retrying tomorrow.

**Status:** fixed by C6 on 2026-09-16. Throttled details are errors, so the run
records `failed`; the sweep holds while any due procedure failed. Remove this
entry once C6's production check passes.

## W25. Safety Gate can checkpoint an unexpected XML document as an empty report

**Priority:** before launch. **Evidence:** reproduced and code review. Files:
`src/safety_gate.py:parse_report_detail`, `run_safety_gate_collection`,
`run.py:cmd_collect_safety_gate`, `health/analyze.py`.

`parse_report_detail` validates XML syntax and fields of any notifications it
finds, but does not require the expected report root/structure. Reproduced with
`<error>temporarily unavailable</error>`: it returns `[]`; two fixture reports are
counted successful and the watermark advances to the last report. Routine
incremental collection will not revisit either. This is a synthetic response-shape
failure, not a claim that the live API served it.

Thrown report-fetch errors already hold the watermark; that part is working.
But partial detail failure with other reports succeeding can still exit 0, and
Safety Gate is absent from the analyzer's configured collector entries. Its
up-to-date path and a listing failure also do not create a `run` row. Production's
latest stored Safety Gate run is September 11 even though later daily checks were
attempted. Therefore simply adding a 48-hour run-age rule would create false alarms.

**Status:** fixed by C6 on 2026-09-16. The document
root and `report_date` are required; every check writes a run (`zero` when up to
date); the analyzer judges Safety Gate. A partial failure still exits 0 by the
batch convention, but its run is `failed` and now raises a health warning. Remove
this entry once C6's production check passes.

## Remaining quality and report decisions

These are retained because they are unresolved, but they should not expand the
crawl stabilisation work into a report rewrite.

### W3 / W9. Asserted publication dates can move

The September 15 version audit measured forward movements in news-sitemap dates
(WELT 259 identities, WiWo 58, Handelsblatt 33, FAZ 11), feed dates (ZEIT 15,
Tagesschau 12), and page dates (e-commerce Magazin 8, all moving more than a day).
These are historical counts, not a new September 16 measurement. Date-only
version churn for existing title-only identities is fixed. Full-text rechecks,
retitles and changed URL aliases still require a publication-date policy.

Treat `page`, `feed` and `news_sitemap` as provenance of a publisher's assertion,
not proof it never changes. Keep first seen, asserted publication and observed
update separate. Investigate an earliest-trusted-article-date rule, but retain
original values and handle corrections; never apply it to DIP/EP step dates.
Sample t3n/etailment/e-commerce Magazin full-text date-only versions. The old
CLAUDE.md claims that feed/news dates cannot be restamped have been corrected.

### W10. Renamed article URLs remain separate identities

The September 15 audit found 146 Handelsblatt articles with 259 extra URLs,
44 FAZ / 87, 69 WiWo / 84, 53 Spiegel / 53, 12 SZ / 39 and 9 t3n / 9.
The external id is still the URL. No duplicate body-gate decision had been observed
in that sample; repeated selection/alert/reporting remains a risk, not a measured
count of duplicate emails.

For launch, audit verified publisher ids within each source and review duplicate
outputs. A non-destructive grouping key is preferable to immediately merging all
raw identities, queues, decisions and frozen-bundle references. A grouping column
alone would not fix any reader; integrate deliberately if measurements justify it.

### W1 / W15. Undated triage and cumulative exports

The export remains cumulative and triage includes undated candidates each week.
In the first frozen report, 34 of 74 triage items were undated; all were omitted.
That first-window sample cannot validate repeat-week behaviour. Old gated topic
pages can remain in exports after source exclusions stop new collection (the
remaining W5 effect).

Decide a bounded lookback plus evidence required by the prior issue register, and
whether undated triage uses first-seen time or meaningful update time. Preserve
carry-forward's ability to inspect stopped items and complete ledger accounting.
The first bundle was 17.9 MB; backing up every cumulative bundle grows state much
faster than the daily item count. Validate with the second real report cycle.

### W5 / W6. Article quality and undated evidence

Source exclusions fixed the known BPEX and section-index cases; the generic hub
gate remains a heuristic. On sources whose bodies normally have page dates it
rejects bodies without one, before model review. Sample both these rejects and
successful extractions, especially after publisher template changes. A real
article losing date markup must not be assumed to be a listing. Small association
sources may never meet the heuristic's minimum sample size.

Bundesnetzagentur URL dates remain an optional source-specific enrichment, not a
reason to drop undated records or infer dates generically. Keep the outstanding
Verbraucherzentrale sidebar/evergreen and teaser-quality audit in `todo.md`.
PDF bodies are capped at 20 pages; unusually long or partial evidence needs review
before it is treated as a complete source.

### W7 / W17. Yield and customer alert scope

Re-measure source value after four healthy weekly cycles. The old first-week
figures mix initial backfill, outages and one report and cannot justify pruning.

The customer's alert taxonomy still does not necessarily route customs,
de-minimis or parcel-tax stories without an own-brand/alert-term match. The
September 15 sample had 43 admitted customer-brand items without an alert trigger,
mostly legitimate newsletter material. Confirm the desired alert scope with the
customer before promising same-day coverage of those measures.

### Residual report verification decisions

The implemented W12 warning and W13 treatment downgrade are removed from the work
list. Their narrower unresolved choices remain: whether a cited historical item
should be labelled `background_only`, and whether the challenge step can correct
an issue headline as well as its factual fields. Keep these in report validation,
not the collection launch blockers.
