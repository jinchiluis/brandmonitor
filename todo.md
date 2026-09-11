# Brandmonitor active backlog

This file contains unfinished work, unresolved decisions, and operational hazards.
Completed implementation and measurements live in the linked permanent documents.

Authoritative references:

- `mvp_plan.md` — current pilot scope and completion criteria
- `docs/source_coverage.md` — source audits, measured yield, and paywall evidence
- `docs/body_collection.md` — body-fetch commands, storage, and retry semantics
- `docs/selection_and_assessment.md` — selection rationale and assessment funnel
- `docs/source_audit_prompt.md` — repeatable source-audit method

## Current state

Working: news and regulatory discovery, SQLite storage/versioning, durable body
queue, public/PDF/browser extraction, source path policy, DSA daily aggregates,
client profile loading, deterministic body-aware candidate selection, the title
gate (a cheap LLM check on the day's new title-only candidates, `run.py gate`), and
the body gate (a cheap LLM relevance check on every selected news and regulatory
body, `run.py body-gate`, decisions in `assessment`).

Not built: title-only fetch-on-match, full LLM assessment, immediate alerts, and
report generation. The `report` table is empty; `assessment` holds only body-gate
decisions so far.

Explicitly outside the current build: customer complaints and service-quality
monitoring from reviews/comments. Those require a separate social/review collection
path and must not be implied by the news-monitoring deliverable.

## 0. Finish and validate body coverage

- Sample the **page-dated** bodies for quality: navigation, teaser or login copy
  leaking into an otherwise fine article. The 2026-09-11 pass covered only the rows
  without a page date (all classified, see `docs/source_coverage.md`); the 1,966
  page-dated rows have not been sampled for body quality.
- Residue the hub gate and config do not reach: Verbraucherzentrale `/wissen`
  evergreen pages that slip in under the narrowed `abzocke` prefix. Known false
  positives of the gate: four LOGISTIK HEUTE editorial newsletters and one ohn page
  whose templates state no date. Decide whether editorials should be excluded by
  config instead of quietly skipped.
- Strip Verbraucherzentrale's sidebar teaser block at extraction, as the LOGISTIK
  HEUTE tag cloud is stripped. It sits on 158 of 203 case pages, 9 of them hold
  nothing else, and its product-recall blurb made the body gate keep unrelated
  cases.
- bevh `/positionen` is kept in `allowed_dirs` on the section name alone: in the
  stored window it held only the AI whitepaper chapters (navigation-only HTML,
  correctly `unavailable`) and three 315-character quiz pages. Drop it if a real
  position paper never shows up.
- ~~Re-run selector recall measurement after body coverage is representative.~~
  Done 2026-09-10 on 2,336 bodies: 604 of 1,148 full-text selections are found only
  in the body, 112 of them brand or topic finds. Figures and the labelled examples
  are in `docs/selection_and_assessment.md`. Partly answered 2026-09-11: in 30
  random selected bodies, 1 of the 22 keyword-only picks was relevant (labels in
  `clients/jt-express/labels/`).
- Re-run that recall measurement once with the hub gate on: 24 rows drop out of
  the eligible set, and the keyword-only body finds were the class most likely to
  have come from concatenated teasers.

## 1. Complete the MVP analysis path

### Report sketch and assessment schema

- Sketch the weekly Chinese report before finalizing the assessment output. Include
  executive summary, regulatory developments, reputation/market items, actions,
  source coverage, and explicit source failures/zero-yield sources.
- Select the full-assessment model. Relevance is its own cheap stage, the body gate
  below, not a field of the full assessment.
- Define structured assessment output from what the report actually needs. Every
  assessment must retain client, prompt version, profile version, and source version.
- Decide when a profile-version change triggers reassessment of historical items.

### Title-only fetch-on-match

- Allow an explicit selected URL to enter the body queue regardless of its source's
  `content_mode`. Do not turn mainstream sources into bulk full-text sources.
- Queue the title gate's survivors for that fetch. `run.py gate` decides and logs
  but queues nothing yet, and `run_body_fetch` still takes only `full_text` sources.
- Integrate paid accounts only for this small fetch-on-match set and only after the
  subscription-value and terms review below.

### Body gate

Built 2026-09-11; design and figures in `docs/selection_and_assessment.md`.

- Drain the backlog: on 2026-09-11, 669 news and 49 regulatory bodies had no
  decision. The daily run gates 300 per tier, so news clears in about three runs;
  `body-gate --limit 700` clears it in one (about a million input tokens).
- During the pilot, skim the gate's drops weekly - `relevant = 0` under a
  `body_gate-%` prompt version. A wrong drop never reaches the full assessment.
- Now and then, and whenever the Bundesnetzagentur publishes a postal decision,
  gate the regulatory bodies the selector skipped once and read what it keeps. That
  is where missing vocabulary (*Porto*, *Briefentgelt*, *Deutsche Post*) would show.
- The legal-Q&A body false match does not bite: both returns Q&As in the labels are
  kept as parcel liability. Leave it unless the full assessment's volume says
  otherwise.

### Client questions still required

- Does the September 10 keyword list extend or replace the earlier customs, GPSR,
  de-minimis, and DSA topics?
- Is "local logistics industry" meant as parcel and e-commerce logistics, or does
  the customer also want port strikes, truck tolls and rail funding? The profile
  assumes the former; the measured cost of the latter is in
  `docs/selection_and_assessment.md`.
- What does `01519` identify?
- What must trigger an immediate alert, and what delivery time is promised?
- Confirm product/material categories, sourcing countries, EU legal role, and any
  company-size thresholds needed for regulation assessment.

### Implement and validate

- Implement the full structured assessment.
- During the pilot, skim the title gate's dropped items weekly in
  `data/title_gate/<client>/`. A wrong drop is recorded nowhere else.
- Implement report rendering and immediate alert output after their schemas settle.
- Hand-label a small relevant/irrelevant sample and measure false positives and false
  negatives before sending a customer deliverable. The 90 body-gate labels in
  `clients/jt-express/labels/` cover the gate, not the full assessment's output.
- Run one fixed pilot window end to end twice. Verify no duplicate raw items or
  assessments, complete source accounting, stable versions, runtime, and LLM cost.

## 2. Validate whether Safety Gate is useful to J&T

- Hand-review 15–20 Safety Gate alerts as `direct`, `customer exposure`, `market
  trend`, or `irrelevant`, then confirm the useful product/seller/SKU/shipment scope
  with J&T and decide immediate-alert versus clustered-weekly treatment. A marketplace
  match does not prove J&T carried the item. In the same review, decide whether DSA
  aggregates merit any background coverage; never treat them as item-level alerts,
  and retain their stored CC BY 4.0 attribution if they appear in customer output.

## 3. Remaining source and subscription decisions

- Review the retained Verbraucherzentrale event sections after real assessment data
  exists.
- Before buying or integrating any subscription, ask what is already active and what
  it costs. Audit only client-relevant title hits for unique paid evidence, permitted
  automated access, integration effort, and expected useful items per month. Then
  choose `subscribe`, `title-only/manual`, or `drop`; paid access remains
  fetch-on-match rather than a bulk archive crawl.

## 5. Dates, scheduling, and operations

- Decide how title-only sources obtain or validate publication dates. Their pages are
  not opened during discovery, so a future CMS restamp could go undetected. The
  full-text tier has the same problem for a different reason — see §6.
- Surface undated records for human review in weekly reports. Do not silently drop
  them: BNetzA and BPEX can carry real signal without usable dates. Now measured:
  67 and 47 undated body rows respectively, and for BPEX that is frontpage discovery,
  which carries no date by construction.
- Schedule collection daily on the Windows laptop. News sitemaps retain titles for
  roughly 48 hours; missed runs permanently reduce title coverage.
- Verify laptop-to-VPS backups. A fresh heartbeat says the pipeline ran, not that
  the database is recoverable; check those separately.

### VPS check on the scheduled laptop run

`run_daily.bat` writes `data/last_run.json` after every completed run: finish time,
per-stage exit codes, and the worst code. The VPS reads that marker and decides
whether a human should look. It stays pull-based; the VPS must not run a second
scheduled collection pipeline.

Freshness is the primary alarm, not the exit code. The worst failure — the run did
not happen, because the laptop slept, lost network, or the scheduled task stopped
firing — produces no process and therefore no exit code at all. Missed news
collection is unrecoverable: news sitemaps retain titles for roughly 48 hours.

- **Alarm on staleness first.** No marker newer than about 26 hours is an alarm on
  its own, regardless of what the last marker said.
- **Then on exit 2** from any stage: the command aborted — missing credential,
  unreadable source list, locked database, unhandled exception. Exit 1 (every
  source in that stage failed) is worth a look the same day. Exit 0 with individual
  sources down is not an alarm; that is what the run summary and `run_source` are
  for. The full contract is in the `run.py` module docstring.
- **Then on trends across runs**, which is where the real signal lives and which no
  single-run exit code can express:
  - a source at `zero` for K consecutive runs — dead, but never "failed" on any one
    day;
  - a source that failed on this run *and* the previous one, as opposed to one blip;
  - an `unavailable` rate that jumps for one source — 5% to 90% means the extractor
    broke, which is exactly the VerkehrsRundschau signature and went unnoticed for
    the whole time the old exit semantics were returning 1 every day;
  - `deferred by limit` recurring, which means the body queue is falling behind
    rather than failing.
- Implement that as `run.py health` over `run` and `run_source`, run on the laptop
  and folded into the marker. The VPS then needs no database access, no schema
  knowledge, and no Python.
- Transport: the VPS already holds admin SSH to the laptop (`contabo-server` key),
  so it can pull the marker over the existing channel.
- Add only the paywall credentials justified by the subscription audit and the LLM
  keys chosen for assessment.
- Record runtime and variable cost for the complete pilot cycle.
- Track the Bundestag DIP API key expiry in May 2027 only if that source returns to
  scope.

## 6. Dates: what is still open

The extraction, provenance, fixture and refresh work landed 2026-09-11; the
measured picture and the rules are in `docs/source_coverage.md` ("What the
non-page-dated bodies actually were") and `docs/body_collection.md` ("Page dates").
The short version: of the 444 rows once blamed on the extractor, about ten were the
extractor, ~215 were hub pages, 202 were Verbraucherzentrale case records with no
publication date to find, and the rest were feed-dated and fine.

Decisions taken, recorded here so they are not re-litigated:

- **Regulators and associations without a structured date stay undated.** They are
  not daily news, and assessment reads the dates in the body. No URL-date rule for
  Bundesnetzagentur, no listing-date scraping for BPEX.
- **Verbraucherzentrale `lastmod` stays stored as a change signal**, labelled
  `lastmod`, never printed as a publication date. The case's own dates are in the
  body for the assessment to read.
- **htmldate is not used for stored dates.** It is installed as a trafilatura
  dependency and its fast mode gets several templates right, but it reads text
  from any element whose class contains "date": it confidently dated every hub
  page from teaser dates and took a class action's filing date as publication.
  It may be useful as a probe diagnostic, nothing more.

Still open:

- Sources collected before 2026-09-11 carry `published_at_source = discovery` on
  their older rows; readers resolve it from `discovered_via`, but a sitemap row's
  flavour (news sitemap or `lastmod`) is unrecoverable. It corrects itself as rows
  are refetched; a one-off relabel is not worth a migration.
- `title_only` sources now record their discovery label, but a title-only source
  whose sitemap is restamped still has no page to contradict it. §5 still applies.

### Extend `probe` to open article pages — for new sources only

`src/probe.py` deliberately stops at discovery: it reports which methods work and how
URLs divide across prefixes, but never opens an article. So a new source can be added
with confident-looking `allowed_dirs` and still yield undated teasers — which is how
Verbraucherzentrale got 204 rows with 204 `lastmod` dates. Neither of the 2026-09-11
protections covers onboarding: the hub gate needs 20 stored bodies before it judges a
source, and the config narrowing was only possible because the rows already existed.

Build it the first time a new **crawled** source is added; nothing is waiting on it
now (Safety Gate is an API). Scope it to what 2026-09-11 measured as useful. Per path
prefix, fetch two or three of the URLs the probe already found and print:

- **which mechanism dated the page**: JSON-LD, meta, lone `<time>`, or none. This is
  the one signal that separated every hub in the sample, and it decides whether the
  selector gate will ever apply to the source.
- extraction outcome: text, no text, paywall, login wall.
- body length, so identical furniture pages (the eight 885-character VerkehrsRundschau
  sections) stand out.

Dropped from the earlier plan: date-stamp density and length-to-median ratio. Both
catch only the big LOGISTIK HEUTE hubs and miss the small section indexes. The output
is a table to read, not a verdict: BVDW's WordPress dates every page, so "dated" alone
never proves "article". htmldate may run here as a diagnostic column, never stored.

### Stored-row audit for existing sources: `src/audit.py`, wired as `run.py audit`

The 2026-09-11 narrowing was done with three throwaway scripts against the database,
and they answered the §3 audit questions better than any live probe could, because
they use every stored row rather than a sample. They should exist as a module, same
shape as `probe`: code in `src/audit.py`, `run.py` only registers the subcommand.
Read-only on the database. Three views:

- **provenance by prefix**, per source: rows and bodies split by `page` /
  `feed` / `news_sitemap` / `lastmod` / undated, with a path-prefix histogram of
  the non-page rows. This is where the hub sections show up.
- **config dry run**: given a proposed `allowed_dirs` / `excluded_dirs` for one
  source, what the change keeps and drops, split the same way, listing every
  page-dated row it would drop. This caught the two Händlerbund press releases and
  BVDW's 89 dated non-articles before the config was written.
- **gate dry run**: which rows the selector would currently skip, with URLs, so a
  threshold or a template change is checked against real rows and not trusted.

Also the restamp check CLAUDE.md prescribes (distinct publication days against row
count, lag from the busiest day) belongs here rather than in a one-off query.

### Independent of all the above

Window the weekly report on `assessment.created_at` or `raw_item.fetched_at`, not on
`published_at`. That is a statement about what the product promises — "what surfaced
this week", which is a fact we own — and it holds even with perfect extraction.
`published_at` stays a display field, omitted or flagged where it cannot be backed.

## Recommended execution order

1. Finish body backfills and sample quality of the page-dated bodies (§0). Date
   extraction and provenance are done; what remains in §6 is the stored-row audit
   module, and the probe extension when the next crawled source arrives.
2. Validate Safety Gate signal and decide whether DSA belongs in the product (§2).
3. Sketch the report and lock the assessment schema.
4. Implement title gating, fetch-on-match, and full assessment.
5. Render the Chinese report and validate one repeatable pilot window.
6. Audit subscriptions before purchasing or integrating another account.

My own comments (not written by claude):
- a cheap body selector??
-> cheap summarize without losing info
-> cheap assess if it has something to do with J&T at all..
- Basic news sites like Spiegel, Zeit etc... almost contain no signal at all. That means IF they show stuff.. its probably important (and have big coverage) and should be weighted a bit more (belongs into llm assessment)
- for alerts i wanna try wechat over ServerChan, thats a cool feature especially for chinese clients
