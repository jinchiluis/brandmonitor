# Brandmonitor active backlog

Unfinished work, unresolved decisions, and operational hazards. Nothing already
built or already decided belongs here — that lives in the permanent documents
below, and a decision recorded there is not re-litigated here.

Authoritative references:

- `mvp_plan.md` — current pilot scope and completion criteria
- `docs/source_coverage.md` — source audits, measured yield, and paywall evidence
- `docs/body_collection.md` — body-fetch commands, storage, page dates, retries
- `docs/selection_and_assessment.md` — selection rationale and assessment funnel
- `docs/source_audit_prompt.md` — repeatable source-audit method

## Current state

Built and running daily: news and regulatory discovery, SQLite storage and
versioning, the durable body queue, public/PDF/browser extraction, source path
policy, DIP and EP procedures, EU Safety Gate, the deterministic selector, the
title gate, title-only fetch-on-match, the body gate, the DIP documents behind
the procedures it keeps, and the daily news alert gate. The alert gate runs last,
reads only news admitted by the relevance gates, and sends at most one combined
Chinese email directly from the laptop. DSA aggregates are collected by hand,
not daily.

Built and run by hand, weekly: the report stack in `src/report_agent/` —
`run.py export-window / assess / report / verify-report`. It produces a Chinese
weekly report, an editorial ledger accounting for every identity, a coverage
page and a verification file. It is validated structurally, not yet by
measurement; see §1.3.

Alert decisions live in `alert_decision`, not `assessment`: alerting is an action
on top of weekly eligibility and must not overwrite a relevance decision. Delivery
is internal-review only for the pilot. The live laptop still needs its SMTP values
and one real end-to-end email test before this path is operationally proven.

Outside the current build: customer complaints and service-quality monitoring from
reviews and comments. That needs a separate social/review collection path and must
not be implied by the news-monitoring deliverable.

## 1. The analysis path — the whole remaining MVP

### 1.1 Assessment cadence and windowing — decided 2026-09-12

The frame that settles this: **only the network stages are irreversible.**

| Stage | Reversible? | Cadence |
|---|---|---|
| Collection | **No** — news sitemaps hold titles ~48 h; a missed day is gone | daily |
| Body fetch | **Mostly not** — pages get pulled, paywalled, restamped | daily |
| Gates | Yes, cheap, read-only on the database | daily |
| Full assessment | **Completely** — reads only the database | weekly |

The assessor never touches the network, `raw_item` is immutable and versioned, and
bodies live in its payload. So an item collected on Monday can be assessed on
Friday, next month, or three times under three schemas, with nothing lost. The
`assessment` key `(raw_item_id, client_slug, prompt_version, profile_version)`
makes re-assessment additive rather than destructive — already demonstrated in
practice, since two regulatory gate prompt versions coexist in the table today.

**Run the full assessment weekly, not daily.** Three reasons, in order of weight:

1. The schema is not settled. Every daily run under a schema that will change
   produces rows to discard at real cost — roughly 4 runs before the first customer
   report instead of 28.
2. Part of what the report must say is only visible across a week. Three sunscreen
   recalls in one week, all AliExpress, is a sentence no assessor looking at one
   item at a time can write; daily assessment would need a second clustering pass
   anyway.
3. Version churn. 3,051 of 18,379 news items carry more than one version (max 4),
   and DIP re-indexing adds roughly 65 a day. Assessing daily assesses an
   intermediate version of something that changes again before the report.

Cost is not the deciding factor: steady state is ~25 news and ~5 regulatory bodies
a day reaching the gate, which keeps about a third, so the assessor's weekly input
is roughly 70 items plus ~4 Safety Gate alerts.

**Window on `raw_item.fetched_at`, not `assessment.created_at`, and never on
`published_at`.** `fetched_at` is populated on 100 % of 25,339 stored rows;
`published_at` is missing on 2.6 %. Windowing on when something *surfaced* is a
fact the product owns, so undated sources cost a display field and never a window.
`created_at` is rejected specifically because a re-run or a retrospective pass
stamps everything with the day it ran, making the window irreproducible — the
property this section exists to protect.

Consequences to build to:

- **Do not put the cadence in the code.** `run.py assess --since --until --client`
  takes an explicit window and skips anything already assessed at the current
  prompt and profile version. Daily versus weekly then becomes one line in
  `run_daily.bat`, changeable after the first pilot cycle.
- **Own-brand alerting is decoupled from assessment cadence.** The taxonomy marks
  `own_brand` as an alert push, and a weekly assessment cannot deliver a same-day
  alert. The daily news alert gate takes the selector's `role: own` matches plus
  category 4/5 term hits, asks only whether each is a potential alert, and sends
  one combined email to the human reviewer. The full assessor is not in that path.
- **Risk accepted:** a weekly run that fails on report day leaves no slack, where a
  daily one would have surfaced the failure earlier. Acceptable while the pipeline
  is run by hand; once scheduled, run the assessment the day before the report.

### 1.2 Assessment schema — settled 2026-09-12

The report sketch and the schema it implies are done and built
(`report_plan.md`, `docs/selection_and_assessment.md` "Weekly assessment and
report"). The treatment vocabulary, the issue register and the tool surface are
fixed; what is left is measurement, below.

Still open:

- Decide when a profile-version change triggers reassessment of historical items.
  The bundle records the profile version it froze and `assess` warns when the two
  differ, but nothing re-opens an earlier cycle.
- Alert types: the deterministic `matches` block plus assessor-assigned
  `alert_types` are still unimplemented — the register carries `use` and
  `next`, not a taxonomy tag. For the alert-word audit list, give the taxonomy's
  German terms a `*` suffix first; whole words miss *Bußgelder*.
- Own-brand alerting stays out of this stage, per §1.1: the deterministic
  selector carries it daily.

### 1.3 Validate the report stack

Built, and not yet trusted. In priority order:

- **Run-to-run variance in the report's shape.** Two runs over the same frozen
  week gave the same findings but split them very differently between numbered
  sections and the watchlist table (10 sections versus 4 plus 6 watchlist rows).
  Either the split needs a rule instead of the writer's judgment, or the
  variance needs measuring over several runs. Nothing else on this list matters
  as much for a weekly deliverable.
- **Cost and runtime per cycle are unmeasured across cycles.** One run is ~15
  minutes, 380k input and 55k output tokens; each bundle's
  `assessment-manifest.json` has the per-step split. No price is pinned for
  `gpt-5.6-sol`, so `report_plan.md` §8 stays open.
- **Week-over-week detection is unproven.** Collection began 2026-09-09, so no
  real earlier window exists to carry forward from and the first cycle has an
  empty register by construction. Carry-forward is covered by unit tests with a
  synthetic prior bundle; the first genuine test is the second real cycle.
- **Recall is unmeasured.** Hand-label a relevant/irrelevant sample and measure
  false positives and negatives before sending any customer deliverable. The
  labels in `clients/jt-express/labels/` cover the gates, not this stage's
  output, and triage is the step that can lose an item silently.
- **Chinese output quality has had one model-written, one-pass review.** That is
  the pilot arrangement, not the target.
- **Where the human review gate sits.** For the pilot, mandatory review before any
  client sees a report, with the reviewer marking which items changed a decision,
  which were excessive, and which expected developments were absent.
- Feed parliamentary documents in properly: a DIP procedure reaches the assessor
  through its record, but `procedure_excerpt` from `src/dip_documents.py` is not
  yet a tool, so a long Drucksache is only reachable as stored text.
- Safety Gate items reach the assessor through the client view and cluster like
  anything else. Confirm on a real cycle that the grouped block the customer
  needs — key-customer items named individually, the rest by product class and
  risk — actually comes out of clustering rather than needing its own rule.
- Run `python run.py alert-gate --dry-run` on the live laptop, configure its SMTP
  values, then prove one real combined email. No customer receives this directly.
- Run one fixed pilot window end to end twice. Verify no duplicate raw items or
  assessments, complete source accounting, stable versions, runtime and LLM cost.

### 1.4 Client questions still required

- Does the September 10 keyword list extend or replace the earlier customs, GPSR,
  de-minimis and DSA topics?
- Is "local logistics industry" parcel and e-commerce logistics only, or also port
  strikes, truck tolls and rail funding? The profile assumes the former; the
  measured cost of the latter is in `docs/selection_and_assessment.md`.
- What customer-facing alert threshold and SLA follow the daily internal-review
  pilot? The current build promises neither real-time nor direct customer delivery.
- Confirm product/material categories, sourcing countries, EU legal role, and any
  company-size thresholds needed for regulation assessment.
- Safety Gate wording: confirm the report may say *your customer's listing was
  pulled* and never *J&T carried this parcel*. It is the one claim the source
  cannot support — no carrier is named in any alert.
- Safety Gate geography: Germany-notified only yields 1.7 key-customer alerts a
  week. Every notifying country adds 53 more (France 29, Luxembourg 10, Ireland 5),
  and none of the 53 repeat a product/brand pair already in the German set — a
  different, larger set rather than deduplication. Theirs to answer.

## 2. Pilot-period watching

Cheap recurring checks that only pay off once real cycles run.

- Skim the body gate's drops weekly — `relevant = 0` under a `body_gate-%` prompt
  version. A wrong drop never reaches the full assessment. For DIP and EP also skim
  `relevant IS NULL`: there, unsure stops an item.
- Skim the title gate's dropped items weekly in `data/title_gate/<client>/`. A
  wrong drop is recorded nowhere else.
- Now and then, and whenever the Bundesnetzagentur publishes a postal decision,
  gate the regulatory bodies the selector skipped and read what it keeps. That is
  where missing vocabulary shows. Two cases already found: a written question on
  automated stations replacing postal branches (*Postfiliale*, *Universaldienst*),
  and the customs bill's shipment-data duty (*Postsendungen*, *Postdienste*),
  neither selected by any rule. Decide at the next profile revision whether narrow
  postal vocabulary goes in — `postsendung*`, `postdienst*`, never a bare `post*`
  (Postfach, Posten, Postbank).
- After four weekly reports, check the EP source against
  `clients/jt-express/labels/ep_procedures_2026-09-11.json`: a core or moderate
  procedure the gate called irrelevant is a miss, and anything the gate keeps that
  the hand review passed over is worth reading.
- If trade-policy EU documents keep getting through — the gate kept two EU-US
  tariff regulations forwarded to the Bundestag — add them to the profile's
  `regulatory_false_matches`.
- Once the corpus is a year old, watch DIP version churn: re-indexing adds
  abstracts to written questions about a year after their answer, roughly 65 a day,
  each a new version.
- Review the retained Verbraucherzentrale event sections once real assessment data
  exists.

## 3. Body and source quality

- Sample the **page-dated** bodies for quality: navigation, teaser or login copy
  leaking into an otherwise fine article. The 2026-09-11 pass covered only rows
  without a page date; the 1,966 page-dated rows have not been sampled.
- Re-run the selector recall measurement with the hub gate on: 24 rows drop out of
  the eligible set, and the keyword-only body finds were the class most likely to
  have come from concatenated teasers.
- Strip Verbraucherzentrale's sidebar teaser block at extraction, as the LOGISTIK
  HEUTE tag cloud is stripped. It sits on 158 of 203 case pages, 9 of them hold
  nothing else, and its product-recall blurb made the body gate keep unrelated
  cases.
- Residue the hub gate and config do not reach: Verbraucherzentrale `/wissen`
  evergreen pages slipping in under the narrowed `abzocke` prefix. Known gate false
  positives: four LOGISTIK HEUTE editorial newsletters and one ohn page whose
  templates state no date. Decide whether editorials are excluded by config instead
  of quietly skipped.
- bevh `/positionen` is kept in `allowed_dirs` on the section name alone: in the
  stored window it held only navigation-only AI whitepaper chapters (correctly
  `unavailable`) and three 315-character quiz pages. Drop it if a real position
  paper never appears.
- An EP procedure filed in the last weeks can carry no title in any language — 2 of
  224 on 2026-09-12 — so the gate judges a procedure number and answers unsure,
  which for parliamentary items stops it. Relevance is decided once per procedure,
  so a title arriving later changes nothing. Either hold an untitled procedure back
  or re-gate when a title appears; two items justified neither yet.
- Before buying or integrating any subscription, ask what is already active and
  what it costs. Audit only client-relevant title hits for unique paid evidence,
  permitted automated access, integration effort, and expected useful items per
  month. Then choose `subscribe`, `title-only/manual`, or `drop`; paid access stays
  fetch-on-match rather than a bulk archive crawl.

### Deliberately unbuilt

- **Passage-finding inside a long parliamentary document.** One bill showed the
  gap: the customs bill's summary says nothing about the shipment-data duty it puts
  on parcel carriers, a paragraph deep in 1.1 million characters, and profile
  keywords cannot point to it. That is a single case, and the record was not blind
  to it — DIP's descriptors name *Brief-, Post- und Fernmeldegeheimnis*. A found
  passage would be evidence for the assessor, never customer text. Revisit only if
  a pilot assessment of a bill reads too thin to carry; the candidates then are a
  small model reading the document in chunks (~300,000 input tokens for the largest
  bill) or passages around rules that are rare in the document, which needs the
  postal vocabulary in §2.
- **European Parliament documents.** The doceo document server answers 202 with an
  empty body, a bot check. Watch-list procedures reach the assessment on their
  record alone.

## 4. Dates: what is still open

The extraction, provenance and refresh work landed 2026-09-11 and the rules are in
`docs/source_coverage.md` and `docs/body_collection.md` ("Page dates"), including
why regulators stay undated, why `lastmod` is stored only as a change signal, and
why htmldate is not used for stored dates.

- Decide how title-only sources obtain or validate publication dates. Their pages
  are never opened during discovery, so a future CMS restamp goes undetected.
- Surface undated records for human review in weekly reports rather than dropping
  them silently: BNetzA and BPEX carry real signal without usable dates — 67 and 47
  undated body rows, and for BPEX that is frontpage discovery, which carries no
  date by construction.
- Rows collected before 2026-09-11 carry `published_at_source = discovery`; readers
  resolve it from `discovered_via`, but a sitemap row's flavour (news sitemap or
  `lastmod`) is unrecoverable. It corrects itself as rows are refetched; a one-off
  relabel is not worth a migration.

### Extend `probe` to open article pages — for new crawled sources only

`src/probe.py` stops at discovery: it reports which methods work and how URLs
divide across prefixes, but never opens an article. So a new source can be added
with confident-looking `allowed_dirs` and still yield undated teasers — which is
how Verbraucherzentrale got 204 rows with 204 `lastmod` dates. Neither 2026-09-11
protection covers onboarding: the hub gate needs 20 stored bodies first, and the
config narrowing was only possible because rows already existed.

Build it the first time a new crawled source is added; nothing waits on it now.
Per path prefix, fetch two or three of the URLs the probe already found and print:

- **which mechanism dated the page** — JSON-LD, meta, lone `<time>`, or none. The
  one signal that separated every hub in the sample, and it decides whether the
  selector's hub gate will ever apply to the source.
- extraction outcome: text, no text, paywall, login wall.
- body length, so identical furniture pages stand out.

Dropped from the earlier plan: date-stamp density and length-to-median ratio. Both
catch only the big hubs and miss small section indexes. The output is a table to
read, not a verdict — BVDW's WordPress dates every page, so "dated" never proves
"article".

### Stored-row audit: `src/audit.py`, wired as `run.py audit`

The 2026-09-11 narrowing was done with three throwaway scripts, and they answered
the audit questions better than any live probe could because they use every stored
row rather than a sample. Same shape as `probe`: code in `src/audit.py`, `run.py`
only registers the subcommand, read-only on the database. Three views:

- **provenance by prefix**, per source: rows and bodies split by `page` / `feed` /
  `news_sitemap` / `lastmod` / undated, with a path-prefix histogram of the
  non-page rows. This is where hub sections show up.
- **config dry run**: for a proposed `allowed_dirs` / `excluded_dirs`, what the
  change keeps and drops, listing every page-dated row it would drop. This caught
  two Händlerbund press releases and BVDW's 89 dated non-articles before the config
  was written.
- **gate dry run**: which rows the selector would currently skip, with URLs.

The restamp check CLAUDE.md prescribes — distinct publication days against row
count, lag from the busiest day — belongs here rather than in a one-off query.

## 5. Scheduling and operations

- `health/analyze.py`'s coverage baseline has no day-of-week awareness: the
  rolling median (`_source_metrics`, `BASELINE_RUNS = 7`) blends weekday and
  weekend daily counts, and `zero_streak >= 2` fires on any two consecutive
  zero days — which is exactly a normal Sat+Sun for weekday-only trade press
  (BGL, HDE, Haendlerbund, DSLV, BVDW, Wettbewerbszentrale, bevh, DVZ, LOGISTIK
  HEUTE, e-commerce Magazin all zeroed on 2026-09-13, a Sunday). No incidents
  have fired yet because `baseline_state` is still `"learning"`
  (`comparable_baseline_runs` < 7 for every source as of 2026-09-13); once it
  reaches "ready" this will produce a recurring false-positive `zero_streak`
  warning most Mondays, reaching the VPS health emailer. Fix before that: make
  the baseline/zero-streak logic day-of-week aware, or exclude Sat/Sun from the
  streak count for sources with an established weekday-only pattern.
- Verify laptop-to-VPS backups. A fresh heartbeat says the pipeline ran, not that
  the database is recoverable; check those separately.
- Send one `health/check.py --test-email` from the VPS. The checker is live and its
  healthy path is proven, but no alert has ever been delivered, so the SMTP leg is
  the one link a real incident would discover.
- Add only the paywall credentials justified by the subscription audit, and the LLM
  keys chosen for assessment.
- Record runtime and variable cost for the complete pilot cycle.
- The public DIP API key expires at the end of May 2027. Request a personal key
  (dip.bundestag.de/über-dip/hilfe/api) and set `DIP_API_KEY` in `.env` before
  then; a rejected key makes `collect-dip` exit 2.

## Execution order

1. Sketch the report, then lock the assessment schema (§1.2).
2. Implement the full assessment against that schema, windowed per §1.1 (§1.3).
3. Render the Chinese report and validate one repeatable pilot window twice.
4. Body and source quality sampling (§3) — in parallel; none of it blocks the
   assessor.
5. Audit subscriptions before purchasing or integrating another account (§3).

My own comments (not written by claude):
- brightdata fallback in case of blocked crawl/scrape? e-commerce failed fetch is a candidate. part of backup plan we can use in production actually
  (I have already ISP IP with deposit)
- apply backup plan
- a new report must be sent per email / Wechat
