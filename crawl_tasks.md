# Crawl stabilisation tasks

Written 2026-09-15 after checking `weaknesses.md` against the production database,
the run logs, the health snapshots and the VPS checker. Go-live is in about two
weeks, so this is the list of work that makes collection trustworthy by then,
ordered by deadline. Each task is written so that it can be handed to a smaller
model on its own: it names the files, the rule, the tests and the finish line.
Nothing here is decided by being written down; strike what you reject.

Conventions for every task:

- Work on `main` in `c:\apps\brandmonitor`. Run `python -m pytest -q` before and
  after; 527 tests pass at the start (the working tree, not the deployed commit).
- Read `CLAUDE.md` first. Do not touch the database, `.env`, or the laptop unless
  the task says so. `python tools/laptop.py sql "..."` is read-only and safe.
- One task, one commit. The commit message names the weakness (`W16`) or task
  (`T2`) it closes. Do not fold in other cleanups.
- A task is done when its **Done when** line holds, not when the code looks right.

Sizes: **S** is under an hour for a small model with the brief below, **M** is a
few hours, **D** is a decision only the owner can make.

---

## What was found on 2026-09-15 that is not in weaknesses.md

Facts the tasks below rely on. Numbers are from the laptop database at 17:40 CEST.

1. **The laptop still runs `6569fd7`.** Every fix marked *done, not deployed* in
   `weaknesses.md` (W2, W5, W8, W11–W14) is uncommitted. DVZ, VerkehrsRundschau
   and haendlerbund.de have stored nothing new since 2026-09-12, 09-11 and 09-10;
   etailment and BVDW show the same date-only `<lastmod>` pattern (etailment's
   canary is at 4/10, BVDW found 0 in its last 15 runs). Every day without T1
   loses another day of those sources. The VPS did send the DVZ/etailment warning
   (`alert_sent_utc 2026-09-15T04:30Z`, latched as `quality_warning`), so the
   detection path works end to end; the fix is simply not deployed.
2. **The health baselines turn "ready" on 2026-09-19** (three comparable days on
   09-15, seven needed). The first weekend after that is 09-19/20, so on Monday
   **09-21 at 06:00** `zero_streak` and `yield_drop` will fire for every
   weekday-only source (todo.md §5). T2 has that deadline.
3. **W18–W26 do not exist.** The "Crawl audit follow-up" table at the top of
   `weaknesses.md` cites them, but the file ends at W17. Whatever Codex wrote for
   them was lost. Where a table cell was specific enough to verify, it is a task
   below (W19 → T5, W22 → T6, W24 → T3); W20 ("date-based rejection"), W21 ("SZ
   headlines"), W23, W25 and W26 could not be reconstructed and are not
   accepted until someone re-derives the finding.
4. **The EP collector reports `failed` every day and nobody sees it.** Runs 82
   and 114 are `status = failed` because one listed procedure is "unknown to the
   API" (`2023/0208(COD)`, a benign listing mismatch), while the stage exits 0
   and `health/analyze.py` skips collector sources entirely. A real EP outage
   would look identical. T3.
5. **Section index pages are stored items and are re-fetched every run.**
   `etailment.de/magazin`, `/magazin/ki`, `/magazin/logistik`, …,
   `verkehrsrundschau.de/nachrichten`, `/nachrichten/recht-geld`, …: 15 URLs,
   each with 6 stored versions, re-fetched in every one of the 8 daily runs
   because their `<lastmod>` moves. The selector's hub gate keeps them out of the
   gates, so the cost is ~120 fetches a day and version churn, not wrong output.
   T8.
6. **Two title-gate keeps are stuck behind a paywall and reach nothing.** FAZ
   "EU-Zölle auf China-Importe bringen JD.com näher…" and DVZ "DHL und Alibaba
   prüfen KI-Kooperation" were kept by the title gate, their bodies are
   `unavailable`, and the alert gate only reads bodies. Both look relevant. T6.
7. **Every run logs 12 warnings for deleted BVL keeps** ("title-gate keep has no
   raw item"). Harmless, but it buries real warnings. T7.
8. **The W2 rule is safe on the one regression I looked for.** A title-only item
   first stored with a `lastmod` date and later found by a feed with a real date
   still gets its version when the feed carries a title (a new title is a
   retitle); only 95 identities ever showed that upgrade and nearly all are
   pre-09-11 rows without provenance. No task.

---

## T1 — Commit, deploy, recover, verify (owner + any model) — today

**Why first.** Collection is the one irreversible stage. Everything else on this
list can wait a day; this cannot.

**Steps.**

1. `python -m pytest -q` (expect 527 passed). Commit the whole working tree as one
   commit: "W2 W5 W8 W11-W14: collection overlap, retitle-only versions, alert
   gate fail-open, report fixes". Push.
2. `deploy.bat` (pulls the laptop, then the VPS). Check with
   `python tools/laptop.py status` that the deployed commit changed.
3. Recovery, on the laptop, between two scheduled slots (the intraday task runs at
   even hours 08:00–22:00; a manual run must hold `data\run.lock`). The first
   scheduled run after deployment recovers the last 48 hours by itself; this
   step gets back the older losses. In one PowerShell on the laptop:

   ```powershell
   cd C:\apps\brandmonitor
   .venv\Scripts\python run.py collect --kind news --days 7        # note the run id it prints
   .venv\Scripts\python run.py collect --kind regulatory --days 7
   tools\repair_title_gate.bat <news run id>                        # gates the run, fetches keeps, alert gate
   .venv\Scripts\python run.py body-gate --kind news
   .venv\Scripts\python run.py fetch-bodies --kind news --title-gate-client jt-express --refresh
   ```

   The last line re-reads the 15 title-gate bodies and writes the SZ Temu/Shein
   body that a frontpage re-date hid (W2 "Remaining"). Expect a handful of
   alerts about week-old articles; the alert prompt rejects anything older than
   14 days. `collect` is not lock-aware on its own, so run this only when
   `python tools/laptop.py status` reports the lock `free` and the next slot is
   more than 20 minutes away.
4. **Done when** the next morning's `data/health/canaries/latest.json` shows DVZ
   and etailment `healthy` and the VPS sends its recovery email, and
   `python tools/laptop.py sql "SELECT source_slug, MAX(fetched_at) FROM raw_item WHERE version=1 GROUP BY 1"`
   shows verkehrsrundschau.de, haendlerbund.de, dvz.de and bvdw.org with today's
   date.

**Watch for a week afterwards** (`weaknesses.md` W8 side effects): the body-fetch
line in `run_daily.txt` ("N bodies ok, M new versions") should not climb much
above today's 10–28 per run, and the `alert_gate` stage may now exit 1 when one
model call fails outright (W11), which the VPS reports as a failed stage. If that
becomes a daily email for transient errors, it is a tuning task, not an outage.

---

## T2 — Weekend-aware zero streak in the health analyzer (S) — before 09-21 06:00

**Files.** `health/analyze.py` (`_daily_periods`, `_source_metrics`),
`tests/test_health_analyze.py`.

**Problem.** Periods are 24-hour buckets counted back from the latest run's
window end (06:00 Berlin = 04:00 UTC). A bucket covers one calendar day. Trade
press publishes nothing on Saturday and Sunday, so every Monday the two trailing
buckets are `zero`, `zero_streak` reaches 2 and `yield_drop` sees two days at
zero. Both rules fire for every weekday-only source once its baseline is ready.

**Rule to implement.** A period whose covered day is a Saturday or Sunday in
Europe/Berlin is neither counted in `zero_streak` nor in the `yield_drop` pair
nor in the baseline median, for every source. Compute the covered day as the
Berlin date of the bucket's end minus one minute (a bucket ending Monday 04:00
UTC covers Sunday). Weekday zeros still count exactly as before, so a source
that goes quiet Wednesday and Thursday still warns on Friday morning. Do not add
per-source configuration; a mainstream outlet loses two of nine baseline days
and nothing else.

**Tests.** Add to `tests/test_health_analyze.py`: (a) a source with ok
Mon–Fri, zero Sat+Sun, ok Mon produces no incident on Tuesday's run; (b) the
existing `zero_streak` and `yield_drop` tests still pass when their zero days are
weekdays; (c) a source zero on Thu, Fri, Sat, Sun, Mon reports `zero_streak` 3 on
Tuesday, not 5.

**Done when** the tests pass and `python health/analyze.py --cycle-date <today>`
run against a copy of the production database (`data/backups/*.db.gz`, unpacked
into a scratch path and passed with `--db`) lists no weekend-caused incident.
Deploy with T1's mechanism.

---

## T3 — An unknown EP procedure is a skip, not a failed run; health sees collector runs (S)

**Files.** `src/ep_procedures.py` (around line 420–460), `run.py`
`cmd_collect_ep`, `health/analyze.py` `_source_entries` /
`_source_metrics`, `tests/test_ep_procedures.py`, `tests/test_health_analyze.py`.

**Part A.** "listed but unknown to the API" is recorded today in
`summary["errors"]`, which marks the source result and the run `failed`. Move it
to its own list `summary["unknown"]` (the counter already exists), print it in
the run note ("1 listed but unknown to the API: 2023/0208(COD)"), and let the
run be `ok` when the only problems are unknown procedures. An HTTP or parse
error on a procedure that the API does know stays an error. Exit code: keep
`1 if errors and not fetched`, and add `2` when the listing itself could not be
read, so the batch stage and the VPS see a dead API.

**Part B.** `health/analyze.py` builds its expected sources with
`include_collectors=False`, so DIP, EP and Safety Gate runs are invisible to it.
For each collector entry, look up the latest run of its own kind (`dip`,
`ep_procedures`, `safety_gate`) and raise `source_failed` (severity `warning`)
when that run's status is `failed`, and `missing_collection_run` when there is
no run of that kind within 48 hours. Do not apply the volume rules
(`zero_streak`, `yield_drop`) to collectors; a procedure list is quiet by nature.

**Tests.** (A) an unknown procedure leaves the run `ok` and is named in the note;
a 500 on a known procedure still fails it. (B) a `failed` `ep_procedures` run
appears in `incidents`; an `ok` one does not.

**Done when** tomorrow's `data/health/latest.json` contains a `sources` row for
`oeil.secure.europarl.europa.eu` and run 114's condition would have produced no
incident.

---

## T4 — Independent canaries for the sources that lost articles (S, needs network)

**Files.** `health/canaries.json`, `health/canary.py` only if a check kind is
missing, `tests/test_health_canary.py`.

**Add checks** for `verkehrsrundschau.de`, `haendlerbund.de`, `bvdw.org`,
`e-commerce-magazin.de`, `t3n.de` and `verbraucherzentrale.de`. For each: read
`robots.txt` and the site's sitemap index by hand (`curl` or the browser), pick
the one endpoint that lists recent articles (a news sitemap, the newest article
sitemap page, or the RSS feed), and copy the `exclude_path_prefixes` from the
source's `excluded_dirs` in `input/germany_medias.json` or
`input/regulatory_sources.json`. Use `reconcile_recent: 10`,
`minimum_database_coverage: 0.7`, `coverage_severity: warning`,
`failure_severity: warning` (only the four existing checks stay `critical`).
Keep the canary's own parser; do not import `vendor.newscrawler` into it, since
a shared parser reproduces the same omissions.

**Grace.** An article listed in the last two hours may legitimately not be stored
yet. If `canary.py` has no age floor, add `ignore_newer_than_hours: 3` to the
check schema and skip such entries from `recent_considered`; otherwise use the
existing one.

**Done when** `python health/canary.py --cycle-date <today> --output-dir
<scratch path>` run on the laptop (via `python tools/laptop.py ps`, with the
scratch path so the production snapshot under `data/health/canaries/` is not
overwritten) reports the new checks, each either `healthy` or with a
`missing_urls` list that T1's recovery explains.

---

## T5 — Say so when a sitemap traversal hit its cap (S) — W19

**Files.** `vendor/newscrawler/crawler.py` `collect_from_sitemaps`,
`src/collect.py` `collect_source` / `record_source_result` call,
`tests/test_collect.py`, `vendor/PROVENANCE.md`.

**Problem.** `max_per_source` (2,000 URLs) and `max_sitemap_fetches` (100)
stop the traversal silently. With the 48-hour overlap WELT reaches ~1,200 URLs;
a 7-day recovery run or a busy news day can hit the cap, and the source still
reports `ok`.

**Change.** Have `collect_from_sitemaps` return, beside the hints, whether a cap
was hit and which (`"url_cap"` / `"fetch_cap"`). In `collect_source`, attach that
to the source's result: status stays `ok` but `error` (or a new note column, if
`run_source` has none — prefer reusing `error` with the prefix `truncated:` so no
migration is needed) records `truncated: url cap 2000 reached, N sitemaps
unread`. Log it at WARNING. In `health/analyze.py`, a latest source result whose
`error` starts with `truncated:` raises a `warning` incident `sitemap_truncated`.

**Tests.** A sitemap tree with three URLs and `max_per_source=2` reports
`url_cap`; a tree of 101 sitemap files reports `fetch_cap`; a normal tree reports
nothing. The analyzer test for the incident.

**Done when** the tests pass and a read-only check on the laptop shows which
sources would truncate over a 7-day window: a short script run with
`python tools/laptop.py py -` that loads the news entries and calls
`collect_source(entry, now - 7 days, now, 2000)` for WELT, ZEIT and FAZ, printing
the returned cap flag. `run.py collect` has no dry-run mode; do not add one for
this task.

---

## T6 — A title-gate keep whose body is unavailable still reaches the alert gate (M) — W22

**Files.** `src/alert_gate.py` (candidate selection, `render_item`),
`src/bodies.py` (where `unavailable` is set for a title-gate route),
`tests/test_alert_gate.py`.

**Problem.** The title gate keeps an item, the body fetch ends `unavailable`
(paywall, subscriber login), and the item is never offered to the alert gate
because the gate reads bodies. Two live cases are listed in finding 6 above.

**Change.** When a `body_fetch` row with a `title_gate_routes` entry ends
`unavailable`, the alert gate offers the item with its title, slug, source and
the fetch error in place of the body, and the rendered prompt says the body was
not obtainable so the model judges on the title alone. Store `body: unavailable`
and the reason in the decision payload; the email entry shows "(正文不可用)"
after the title. Treat the item as stopped (nothing further) once decided, like
any other. Do not extend this to full-text sources whose body is unavailable;
they never passed a title gate and that is a separate decision.

**Tests.** A keep with an unavailable body is offered and decided; the decision
payload carries the reason; the same keep is not offered twice; a full-text
unavailable body is still ignored.

**Done when** after deployment `python run.py alert-gate --dry-run` on the laptop
lists the FAZ JD.com item and the DVZ DHL/Alibaba item as candidates.

---

## T7 — Stop warning about keeps of a source that is crawled by nothing (S)

**Files.** `src/bodies.py` around line 733, `src/collect.py` (`crawled_entries`
or a new `discovery_enabled(entry)` helper), `tests/test_bodies.py`.

**Change.** In the title-gate branch of the body fetcher, skip keeps whose source
entry has none of `sitemap`, `feeds`, `frontpage`, `brightdata` set to true,
logging once per source at INFO ("bvl.de is disabled; 12 keeps ignored") instead
of one WARNING per keep. Do not delete anything from the JSONL logs.

**Done when** the `title_bodies` section of `run_daily.txt` no longer shows the
12 BVL lines and the test covers a disabled source.

---

## T8 — Keep section index pages out of the corpus on etailment and VerkehrsRundschau (S)

**Files.** `input/germany_medias.json`, `src/collect.py` `url_is_excluded`,
`tests/test_collect.py`.

**Problem.** Finding 5 above. `is_furniture` cannot catch these (`magazin`,
`nachrichten` are section names, not index markers) and `excluded_dirs` would
remove every article under the section.

**Change.** Add an optional per-source `excluded_url_patterns` list of regular
expressions, matched against the URL path, applied in `url_is_excluded` (so
selection and body backfill honour it too). Entries:

- etailment: `^/magazin(/[a-z-]+)?/?$` — articles are `/magazin/2026-09-14-slug`.
- VerkehrsRundschau: `^/nachrichten(/[a-z-]+)?/?$` — articles end in `-\d{7}`.

Write the reason in each entry's `notes`. Document the key in CLAUDE.md's
"Adding news sources" table in one sentence. Do not delete the stored rows; they
are already kept out of the gates by the hub rule.

**Tests.** The two patterns exclude the listed index URLs and keep one real
article URL from each source; a source without the key is unaffected.

**Done when** the next run's body-fetch log no longer lists those 15 URLs.

---

## T9 — Carry the link text as the title in frontpage discovery (M) — SZ

**Files.** `vendor/newscrawler/crawler.py` `collect_from_frontpage` (line ~1008
builds every hint with `title=None`), `src/collect.py` `_is_retitle`,
`tests/test_collect.py`, `vendor/PROVENANCE.md`.

**Problem.** Süddeutsche is frontpage-only, so all 514 stored SZ items have no
title; the title gate and the report see the URL slug. Words like "Temu" survive
in the slug, so nothing is lost outright, but the reviewer sees
`"bill gates kuenstliche intelligenz regulierung jobverlust china"` instead of a
headline.

**Change.** While scanning a section page, keep the longest anchor text seen for
each kept URL (strip whitespace; ignore texts under 15 characters or equal to
the URL) and pass it as `title`. Because `_is_retitle` writes a version only for
a title no version has carried, existing SZ items gain their headline once on
the next sighting and never churn afterwards; verify that with a test that feeds
the same anchor text twice.

**Done when** `python run.py probe https://www.sueddeutsche.de/ --sources
input/germany_medias.json` prints titles for most hints and the tests pass.

---

## Decisions only the owner can make (D)

Short recommendations so each takes minutes, not a session.

- **W9 (dates that move).** Recommend: for articles, date an identity by the
  earliest trusted date across its versions in both the export and the alert
  gate; keep DIP/EP record dates per version. Then correct the two CLAUDE.md
  sentences the entry names. One implementation task once agreed.
- **W10 (renamed URLs).** Recommend the "lower-risk launch option": compute a
  per-source `article_key` (Handelsblatt/WiWo `/(\d{6,})\.html$`, FAZ
  `-(\d{9})\.html$`, SZ `li\.\d+`, Spiegel `-a-<uuid>`) as an additional column,
  use it to skip an already gated article in the selector and to collapse
  duplicates in the export, and leave `external_id` alone. No migration of
  history before go-live.
- **W1 + W15 (cumulative export).** Recommend bounding the export to identities
  first fetched within 8 weeks plus every id the previous register cites, and
  offering undated items to triage only when first fetched in the window. Decide
  before the second real cycle, since the first cycle cannot show the effect.
- **W17.** A question for the customer: should customs, de-minimis and parcel-tax
  measures be alert types? Send it now; it costs nothing to ask.
- **W7 (source pruning).** Not before four weekly cycles after T1; the current
  figures are contaminated by the outage.

---

## Not doing before go-live, and why

- Rewriting identities (W10 full), passage search in bills, EP documents
  (todo.md "Deliberately unbuilt"), probe extension for new sources: nothing on
  the current source list needs them, and each is a multi-day change into the
  irreversible stage two weeks before launch.
- Re-measuring per-source yield (W7): see above.
- Day-of-week baselines per source or an "expected cadence" floor (W16 second
  bullet): T2 plus T4 cover the concrete failure; a floor that the median cannot
  learn away is a second rule to tune with data from the first quiet week.
