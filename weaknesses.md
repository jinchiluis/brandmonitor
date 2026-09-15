# Weaknesses

Findings from reviewing the pipeline's output. Each entry lists what was measured,
a candidate action, and what that action could break. **Nothing here is decided.**
Go through the entries before implementing any of them; an entry that turns into
work moves to `todo.md`, and an entry rejected on review stays here with its reason.

Status values: `open` (not reviewed), `accepted` (move to todo.md), `rejected`
(reason recorded), `done`. An entry marked **done, not deployed** is fixed and
tested in the working tree but not yet committed, pulled onto the laptop, or run.

Sources for the numbers: the production database on 2026-09-14 and 2026-09-15, the
bundle `data/reports/jt-express-2026-09-05_2026-09-11`, the title-gate logs in
`data/title_gate/jt-express/`, and on 2026-09-15 a 36-hour re-crawl of every news
source that stored nothing, compared URL by URL with the database. One report
cycle and a few days of gating are a thin sample; the per-source figures in W7
especially.

**Task briefs (2026-09-15, evening).** [crawl_tasks.md](crawl_tasks.md) turns the
entries below into self-contained tasks T1–T9 with deadlines, plus the decisions
that need the owner. Note that the follow-up table below cites W18–W26, which
were never written into this file; only the cells specific enough to verify
became tasks.

**Order of work (2026-09-15).** W8 and W2 first, together: collection is the one
irreversible stage and was losing articles daily. Then the session items: W9 and
W10 decide what a new item and its date are; W1 with W15 decide what a bundle
holds; W16 decides how a silent source gets noticed.

### Crawl audit follow-up — 2026-09-15, proposals for discussion

**Scope and confidence.** Reviewed the current working tree, production SQLite
through read-only queries, task markers and existing health snapshots. Ran 395
existing offline tests covering discovery/watermarks, bodies/dates, selection,
gates, structured collectors and health: all passed. Additional mocked failure
cases exposed gaps those tests do not cover. No collection, paid model calls,
deployment, database repair or application-code changes were made during this
audit. W18–W26 below distinguish production observations from reproduced risks.

**Deployment matters.** The production laptop still runs `6569fd7` from September
14. Its September 15 daily and 16:00 Berlin intraday markers both report exit 0.
The W2/W5/W8/W11–W14 fixes remain local. Their passing tests establish the tested
behaviour, not that the running crawl has recovered. No current body-fetch rows
were `pending` or `failed` at inspection; that is reassuring for queue drainage,
but `unavailable` and pre-gate exclusions can still hide relevant material.

**Recommended order, replacing the order above for launch planning.**

| Priority | Proposed work | Reason |
|---|---|---|
| First | Review/deploy W2 + W8 together; make retrieval failures and truncation visible (W18/W19); recover and reconcile the missed window | Stop irreversible loss and establish whether recovery was complete |
| First | Correct W20's date-based rejection, retain SZ headlines (W21), and make EP/Safety Gate failures retryable and visible (W24/W25) | Real articles are excluded; regulatory runs can report success despite missing work |
| Before launch | Extend existing independent canaries (W16); account for unavailable relevant bodies (W22); decide how changed evidence re-enters gates (W23) | A green scheduler and an empty retry queue do not establish usable coverage |
| Before report work | Agree article-date policy (W9), logical identity/duplicates (W10), and undated/week eligibility (W1); label partial text (W26) | Collection time, publication time, update time and evidence completeness must remain distinct |
| Later unless measurements require it | Broad identity rewrites, source pruning (W7), cumulative bundle/backup optimisation (W15), and further report-agent refinements | Preserve the remaining time for observed collection reliability |

**A bounded two-week approach.** Spend the first 2–3 days on loss and visibility
fixes, source-specific recovery and demonstrated false exclusions; use the next
days for handoff/identity/date decisions and focused repairs. Reserve at least the
final seven consecutive days for the same deployed crawl configuration, with daily
reconciliation and review. A critical fix restarts validation for its affected
source/path. This is a proposed schedule, not a claim that all fixes will fit or
that seven quiet days prove completeness.

**Proposed release evidence.** Agree these before calling collection stable:

- Independently enumerate recent article URLs for each priority source, allow a
  documented discovery grace period, and account for every sampled omission.
  Start with DVZ, VerkehrsRundschau, Händlerbund, etailment, OHN, SZ and the
  regulatory inputs. Compare publisher ids where verified; distinguish intended
  section exclusions from missed articles. There should be no unexplained
  relevant omission in the sample.
- No source is treated as complete after a failed or truncated required retrieval.
  Check a normal intraday run, an overnight gap and a recovery window. Inject
  timeout, malformed response, cap and interrupted-handoff cases offline instead
  of waiting weeks for them to occur naturally.
- Every selected article has an explicit outcome: body available, retry pending,
  unavailable with a reason, or a recorded rejection. Review missing bodies and
  rule-based rejects as well as model rejects. Check known own-brand/customer
  stories across the whole path, not only gate-labelled samples.
- An injected source failure reaches the operator's health view in the next
  observer cycle; verify the existing notification path when operational testing
  is authorised. Record discovery lag, oldest unfinished work and unresolved
  source incidents daily. Notification delivery was not tested in this audit.
- Complete a recovery drill into a disposable database and reconcile identities
  and bodies. A successful wide-window command alone is not evidence of recovery.

**Decision to discuss first:** proceed with a small collection-stabilisation
batch and recovery, then measure it, before continuing the report-agent stage.
These are recommendations only; no entry is accepted by being written here.

---

## W1. Undated items are offered to triage every week, indefinitely

**Status:** open — discuss together with W15

**Finding.** The triage pool is every non-DSA candidate whose period is `week`,
`future` or `undated` ([src/report_agent/assess.py:216-217](src/report_agent/assess.py#L216-L217)).
The export takes every gated item ever stored up to the cutoff, not only items
that surfaced in the window ([src/report_agent/export.py:190-201](src/report_agent/export.py#L190-L201)).
`surfaced_in_window` is computed ([export.py:179](src/report_agent/export.py#L179))
but nothing filters on it.

A dated item becomes `history` and leaves the sweep once it is older than the
window. An undated item never gets a date, so it never becomes history. Every
relevant undated item ever collected is offered to triage again in every later
cycle, and the pile only grows.

**Evidence.** In the 2026-09-05..11 cycle, 34 of the 74 items offered to triage
were undated: BPEX 15, BVL 14, Bundesnetzagentur 2, Verbraucherzentrale 2,
Wettbewerbszentrale 1. Triage omitted all 34, correctly: topic pages, old press
releases, archive blog posts. Customer onboarding next week will not age them out.

In that bundle `surfaced_in_window` is true for all 464 evidence rows, history
included, because the whole corpus was fetched inside the first window. This cycle
cannot show what a first-seen filter would remove; the second one can.

**Candidate action.** Offer an undated item to triage only if its **first**
version was fetched inside the window.

**Side effects to check.**
- `surfaced_in_window` uses the *latest* version's `fetched_at`. Filtering on it
  as-is would let a restamp (W2) bring an old item back. First-seen has to be
  computed separately. W2's fix stops re-dated title-only versions, but a body
  version or a retitle still moves the latest `fetched_at`.
- An undated page whose content really changed later would no longer be offered.
  Carry-forward search still reaches it; decide whether that is enough.
- Accounting stays complete: an undated row outside the pool already gets the
  rule treatment `undated_background` ([assess.py:630-631](src/report_agent/assess.py#L630-L631)).
- The first cycle after the change drops the backlog from triage all at once.
  Compare the ledger with the previous bundle to see what that removes.
- This is one symptom of W15: the export is cumulative, not windowed.

---

## W2. A re-dated discovery hint writes a new version of a title-only item

**Status:** done 2026-09-15, not deployed

**Finding.** A title-only item was re-stored as a new version whenever
`sha256(url|title|published_at)` changed. The original entry blamed `<lastmod>`
alone; 2026-09-15 showed the churn is wider and does real harm:

- `<news:publication_date>` moves when WELT, WiWo, Handelsblatt and FAZ update an
  article, ZEIT's feed `<pubDate>` moves the same way, and SZ's frontpage invents a
  23:59:59 date (see W9).
- In the 24 hours to 2026-09-15 that wrote about 500 bodyless versions: WELT 261,
  ZEIT 99, SZ 53, WiWo 37, Handelsblatt 26, FAZ 15, Tagesschau 10.
- **A discovery version can hide a fetched body.** An SZ podcast item about Temu
  and Shein has its body in version 2 and a frontpage re-date as version 3. Every
  stage reads the latest version, so the selector, body gate and alert gate saw no
  body, and an export would list it as `unavailable_body` ("never arrived"). 1 of
  the 15 title-gate bodies fetched so far.

**Action taken.** [`_is_retitle`](src/collect.py#L232) in `store_hints`
([collect.py:394](src/collect.py#L394)): for a title-only item that already
exists, only a title no version has carried yet writes a new version (whitespace
ignored); a missing title never does, and nothing is written once any version
holds a body. The hash formula is unchanged, so nothing is re-hashed and no
migration is needed. Measured against the last day's rows, the rule keeps 27
versions (retitles, mostly liveblogs) and skips 234.

Tests: `test_a_redated_title_only_item_writes_no_version`,
`test_an_untitled_stub_gains_its_title_once_and_never_loses_it`,
`test_discovery_never_writes_over_a_fetched_body` in `tests/test_collect.py`; the
existing `test_changed_title_becomes_a_new_version` and
`test_repeated_runs_do_not_ratchet_versions` still pass.

**Remaining.**
- The SZ item still has its body hidden behind version 3. After deploying, one
  `python run.py fetch-bodies --kind news --title-gate-client jt-express --refresh`
  rechecks the 15 title-gate bodies; `store_body` writes a body version because
  the latest row has none.
- Existing churn rows stay. A title-only item's `published_at` is now the date
  first seen; W9 decides whether that is the date a report should use.
- DIP and EP hash without their paperwork stamps
  (`test_hash_ignores_documentation_stamps_but_not_content`); the full-text path is
  unchanged (`test_lastmod_churn_rechecks_without_content_versions`).

---

## W3. Extra versions in general: mostly useful, one class unexplained

**Status:** open (audit only) — largely answered 2026-09-15

**Evidence.** 24,940 items, 29,819 rows, so 4,879 extra versions. Each version
was compared with the previous one:

| Change from previous version | Rows | Reading |
|---|---:|---|
| Title changed, with or without the date | ~2,980 | Mostly body enrichment: the sitemap stub (bare URL, often no title) becomes headline, text and page date |
| Same title and date | 249 | Body content changed |
| Date only, not `lastmod` | 970 | Largest: WELT 441, t3n 102, SZ 85, ZEIT 68, etailment 66, WiWo 65 |
| Date only, `lastmod` | 682 | Noise, see W2 |

**Why strict URL dedup (first sighting wins) is not the fix.** It would freeze
the enrichment stub, lose real corrections and retitles, lose DIP/EP procedure
steps that arrive under the same id, and break the bundle's "latest version before
the cutoff" traceability.

**Answered 2026-09-15.** For the title-only sources the date-only class is another
restamp pattern, not a correction: WELT, WiWo, Handelsblatt rewrite
`<news:publication_date>` on update, SZ's frontpage dates are synthetic 23:59:59,
ZEIT's feed moves `<pubDate>` (W9). W2's fix stops them.

**Still to sample.** t3n and etailment are full-text sources, so their date-only
versions come from body fetches: a page `datePublished` that moved. e-commerce
Magazin shows the same, 8 of 54 page dates moving by up to 6.9 days (W9).

---

## W4. BVL: high cost, no output

**Status:** done 2026-09-14 (config switched off, stored rows deleted)

**Finding.** BVL (Bundesvereinigung Logistik) is a general logistics and
supply-chain association, not a publisher. It was on the client's first list of
24 outlets and was never chosen on signal.

| Stage | Result |
|---|---|
| Items stored | 621 (577 blog, 41 press, 3 other); 1,243 rows |
| Brand hits | 0 |
| Title gate 2026-09-11 | 93 of 281 titles judged (33%), 12 of 26 kept (46%), all archive posts |
| Bodies fetched | 22 |
| Body gate relevant | 1, which was the `/blog/` index page itself |
| Alerts | 0 |
| Report 2026-09-05..11 | 14 reached triage, 14 omitted |

**Action taken.** In `input/germany_medias.json`, `sitemap` and `feeds` were set
to `false` (frontpage was already off), so the entry is crawled by nothing. The
entry stays in the list with the reason in `notes`. No code changed.

**Stored rows deleted** 2026-09-14, after the config change was deployed
(`6569fd7`) and while holding `data/run.lock`, without a backup (the owner's call:
pre-production data). Deleted: 1,243 `raw_item` rows (621 items), their 2
`assessment` rows, and 22 `body_fetch` rows. Kept: 41 `run_source` rows and the
collection watermark, as collection history. Foreign-key check clean afterwards.
BVL no longer reaches bundles or triage. The frozen 2026-09-05..11 bundle still
contains it, and the title-gate logs still name it. Re-enabling BVL would collect
it from scratch as new items.

**Remaining effects.**
- The health observer records BVL as `zero` every day. Its baseline was still
  learning on 2026-09-14 (3 of 7 comparable days). Nearly all of its days stored
  zero, so the median should be 0 and no `zero_streak` warning should fire.
  **Verify once the baseline is ready.**
- The same mechanism is harmful for a source that breaks rather than one that is
  switched off: DVZ, haendlerbund.de and VerkehrsRundschau learned a baseline of
  zero while they were losing every article (W8, W16).

---

## W5. Frontpage discovery stores topic and listing pages as articles

**Status:** done 2026-09-15 for BPEX (config only), not deployed

**Finding.** A frontpage source keeps every link it finds, and `allowed_dirs`
seeds which section pages are scraped rather than filtering results. On BPEX the
seed sections are stored as items themselves: `themen-und-positionen/zoll`,
`/postgesetz`, `/verkehr-und-umwelt`, `/innenstadtlogistik`,
`/arbeit-und-soziales`, plus `kep-branche/zahlen-und-fakten`. The body gate
marked all 16 gated BPEX items relevant, because a topic page on postal law *is*
on topic.

**Evidence.** 6 of the 15 undated BPEX items in the 2026-09-05..11 triage were
topic or listing pages. The BVL `/blog/` index got through the same way.

**Checked 2026-09-15.** All 58 stored BPEX URLs listed: the 9 under
`themen-und-positionen/` and `kep-branche/zahlen-und-fakten` are all topic pages,
and no article sits below either path. The frontpage collector does not read
`excluded_dirs` at all, so excluding a path filters results without touching the
seeds. The item titled "Meldung" is a real article
(`/aktuelles/meldung/marten-bosselmann-interview-markttrends-2026`) whose heading
extracted badly; it stays.

**Action taken.** `themen-und-positionen` and `kep-branche/zahlen-und-fakten`
added to BPEX's `excluded_dirs`; both stay in `allowed_dirs` as seeds. The reason
is in the entry's `notes`. `excluded_dirs` also applies to selection and body
backfill.

**Remaining.**
- Already gated topic pages stay in every export, which takes all gated items ever
  (W1, W15). The exclusion stops new ones only.
- The general case is still open: the body gate judges topic, not whether a page
  is an article, and the hub gate needs 20 stored bodies with page dates, which a
  small association never reaches.

---

## W6. Dates carried in URLs are not used

**Status:** open (observation)

**Finding.** Bundesnetzagentur press-release URLs carry the date
(`.../Pressemitteilungen/DE/2026/20260706_DSC_ebay.html`), yet both items in the
2026-09-05..11 bundle were undated. The cluster step read the body to place them
in July.

**Candidate action.** Consider a per-source URL date pattern with its own
`published_at_source` value.

**Side effects to check.** CLAUDE.md rules out parsing visible text for dates.
A URL isn't visible text, but a URL date can be a folder or upload date rather
than the publication date, so it needs checking per source. It would also add a
new provenance value that the report rules (which dates may be printed) must
classify.

---

## W7. Per-source cost versus yield

**Status:** open — re-measure about four weekly cycles after W8 is deployed

**Evidence.** Title gate from 2026-09-11, body gate and alerts to 2026-09-14, and
one report cycle. "Report" counts `report` treatments in the 2026-09-05..11
ledger.

| Source | Items | LLM work caused | Output |
|---|---:|---|---|
| etailment | 274 | 164 body-gated | 5 report findings, 1 alert |
| Onlinehändler-News | 169 | 82 body-gated | 5 report findings |
| EU Safety Gate | 639 | client matching only | 2 report findings |
| bevh | 132 | 4 body-gated | 1 alert |
| LOGISTIK HEUTE | 1,054 | 297 body-gated, 53 relevant | 0 (11 brand hits in the 120-day audit) |
| haendlerbund.de | 84 | 62 body-gated, 16 relevant | 0 |
| VerkehrsRundschau | 294 | 52 body-gated | 0 |
| EP procedures | 604 | 224 body-gated | 0 |
| DIP | 1,656 | 52 body-gated | 0 |
| WELT, WiWo, Handelsblatt, Tagesschau | 7,775 | 101 titles judged, 1 kept | 0 |
| BVDW, DSLV, BGL | 130 | 2 body-gated | 0 |

**These figures are confounded (2026-09-15).** haendlerbund.de stored no new
article after 2026-09-10 and VerkehrsRundschau none after 2026-09-11 (W8). Their
"expensive, nothing yet" is the cost of the first 30-day backfill; since then they
have cost nothing because they collected nothing. DVZ is in the same state.

**Reading, not a decision.**
- *Expensive, nothing yet:* LOGISTIK HEUTE, and — once they collect again —
  haendlerbund.de and VerkehrsRundschau. LOGISTIK HEUTE had brand hits
  historically, so one cycle is not enough.
- *Cheap, nothing:* BVDW, DSLV, BGL. Dead weight rather than a burden.
- *Cheap insurance:* mainstream outlets. Under 1% of their items reach an LLM,
  and a large brand story would appear there.
- *Slow by nature:* DIP, EP procedures and the regulators. A procedure moves over
  months, so weekly yield is the wrong measure.

**Side effects to keep in mind.** The CLAUDE.md test for a source is whether it
produces something a weekly report or an alert would carry. Measure over enough
cycles that one quiet week doesn't remove a source a later story needs.

---

## W8. Date-windowed collection silently loses articles

**Status:** done 2026-09-15, not deployed — recovery run and detection (W16) open

**Finding.** Each run kept only sitemap and feed entries dated after the source's
watermark, the start of the previous run. A discovery date is not the moment an
article becomes visible, so any article whose date precedes its appearance fell
before every later window and was never collected. Nothing reported it: the
source's status stayed `ok` or `zero`.

- **DVZ** — its news sitemap states date-only dates, read as 02:00. Anything
  published after the 06:00 run falls before every later window. This was true
  before the intraday runs too; a 24-hour window starting 06:00 excludes that
  day's 02:00. Last new article stored: 2026-09-12.
- **haendlerbund.de** — date-only `<lastmod>`, same effect. Last new: 2026-09-10.
- **VerkehrsRundschau** — the sitemap lists an article hours after its
  `<lastmod>`: it appears to regenerate about every four hours (its section pages
  carry 14:00:41 and are found every other 2-hour run). A 2-hour intraday window
  never contains the date. Last new: 2026-09-11. Its alternating
  "ok, 8 found, 0 stored" are those section pages.

**Evidence.** From the laptop on 2026-09-15, `collect_source` with a 2-hour window
returned 0 hints for all three and a 36-hour window returned their current
articles (DVZ 10, VerkehrsRundschau 24, haendlerbund.de 4).

A 36-hour re-crawl of every news source, compared with the database: 93 of 1,908
hints older than 2.5 hours were missing. 31 were the same article under a changed
URL (W10); 62 were absent — VerkehrsRundschau 15, DVZ 9, etailment 6, t3n 6,
WiWo 6, e-commerce Magazin 4, Handelsblatt 4, FAZ 3, haendlerbund.de 2, LOGISTIK
HEUTE 2, SZ 2, Spiegel 1, Tagesschau 1, WELT 1. The per-item cause of the last
group was not verified; the window mechanism is the likely one.

**Action taken.**
- [`collect_source`](src/collect.py#L291) filters sitemap and feed dates from
  `start - overlap`, with `collection.overlap_hours: 48` in `config.json`. 48
  covers a date-only day plus the observed lag and matches a news sitemap's
  two-day listing. Re-found URLs are skipped by `store_hints`, so the overlap
  re-reads without re-storing. The watermark and the reported window are
  unchanged; frontpage results are not date-filtered and keep the unshifted start.
- **Sitemap cap counted duplicates.** With the overlap, WELT's raw sitemap
  entries reached 2,713, and the 2,000 cap dropped 1 of the latest two hours'
  articles. WELT lists 111 URLs as 307 entries in two hours. `collect_from_sitemaps`
  now counts a URL once and keeps the richer copy
  ([crawler.py:554](vendor/newscrawler/crawler.py#L554), recorded in
  `vendor/PROVENANCE.md`); the same window is 1,223 distinct URLs with nothing
  recent missing.

Tests: `TestWindowOverlap` and
`test_a_url_listed_by_several_sitemaps_counts_once_against_the_cap` in
`tests/test_collect.py`.

**Side effects to watch after deploying.**
- It depends on W2. Without W2 every re-found, re-dated title-only hint in the
  overlap would write a version.
- Full-text sources re-queue a body when a re-found hint's discovery fingerprint
  changed (a moved `<lastmod>`). That happened inside the old window too; watch
  the body-fetch counts for a rise.
- `items_found` per source now includes re-found URLs; `items_stored` is
  unaffected, and it is what the health observer sums.

**Recovery (needs the owner's go-ahead: it writes to the database).** The first
run after deploying recovers the last 48 hours by itself, which covers DVZ as far
as its news sitemap reaches. VerkehrsRundschau and haendlerbund.de lost articles
since 2026-09-10/11. A one-off `python run.py collect --kind news --days 7` under
the run lock, followed by `gate --run <id>` for the title-only arrivals, would get
back what their sitemaps still list. It re-crawls every news source over 7 days.
Recovered full-text articles pass the body gate and the alert gate like any new
item, so the reviewer may get a few alerts about week-old articles; the alert
prompt only rejects articles more than 14 days old.

**Audit qualification.** W19 shows why `--days 7` is not a completeness guarantee:
sitemap URL/traversal caps can truncate that wider window without reporting it.
Prefer recovery restricted to affected sources and bounded intervals, with cap
visibility and an independent URL/id comparison afterwards. Respect the publisher's
remaining archive: a vanished entry cannot be recovered merely by requesting an
older date. Replay affected collection run ids through title gating; its ordinary
latest-run path does not recover old runs.

---

## W9. News-sitemap, feed and page dates are update stamps on several sources

**Status:** open — needs a session (it changes a CLAUDE.md rule)

**Finding.** CLAUDE.md treats `<news:publication_date>` and a feed `<pubDate>` as
dates that "survive a restamp", and the export prints them (`TRUSTED_DATE_SOURCES`
in [export.py:48](src/report_agent/export.py#L48)). Several publishers rewrite them
when they update an article, and one rewrites its page `datePublished`.

**Evidence.** All stored versions compared, 2026-09-15:

| Source | Date field | Identities whose date moved | Moved > 1 day | Largest move |
|---|---|---:|---:|---:|
| WELT | news sitemap | 259 of 2,123 | 41 | 3.8 days |
| WiWo | news sitemap | 58 of 326 | 14 | 3.8 days |
| Handelsblatt | news sitemap | 33 of 586 | 5 | 4.0 days |
| FAZ | news sitemap | 11 of 358 | 2 | 3.9 days |
| ZEIT | feed | 15 of 312 | 2 | 1.5 days |
| e-commerce Magazin | page | 8 of 54 | 8 | 6.9 days |
| Tagesschau | feed | 12 of 137 | 0 | 0.3 days |
| Spiegel | news sitemap | 8 of 299 | 0 | 0.5 days |

Examples: WELT "Das sind die Zuwanderer, die Deutschland braucht" 09-08 → 09-15;
Handelsblatt "Pflegereform – diese Leistungen …" 09-09 → 09-15. The move is bounded
by how long a news sitemap lists an article, about four days.

**Where it does harm.** The export dates an item by its latest version before the
cutoff, so an article updated a few days after publication can move from `history`
into the next week's `week` period and back into triage, with a printed date that
is its update. The alert gate's `PUBLISHED` line has the same exposure. The frozen
2026-09-05..11 bundle has no affected row.

**Already reduced by W2.** Title-only items no longer version on a date change, so
their stored date is the first one seen. What remains: page dates that move on
full-text sources (e-commerce Magazin), and a URL seen for the first time because
its slug changed (W10), whose first news-sitemap date is already an update.

**Candidate actions.**
- Date an identity by the earliest trusted date across its versions, in the
  export and the alert gate.
- Audit each source's trusted fields as the `lastmod` audit did, and correct the
  CLAUDE.md sentences "Both survive a restamp" and "Sources discovered by feed
  carry a real `<pubDate>` and are not exposed to this".

**Side effects to check.** An earliest-date rule would keep a wrong early date that
a later version corrected; count how often a trusted date moves backwards before
choosing. `verify-report`'s `no_untrusted_dates` check and the dates the report
may print follow whatever rule is chosen.

**Audit follow-up, 2026-09-15.** Compared consecutive stored versions with explicit
`page`, `feed`, `news_sitemap` or `record` provenance, normalising timestamps before
comparison. There were no backward changes greater than one day; the 21 backward
changes were feed-to-page on LOGISTIK HEUTE (20) and news-sitemap-to-page on WELT
(1). This supports investigating a conservative first-publication rule for
articles; it does not prove the earliest date is always correct. Older ambiguous
provenance is outside this measurement.

Do **not** apply an earliest-trusted-date rule to all source kinds. DIP had nine
forward record-date transitions and EP four; these represent changing procedure
steps, whose dates must remain attached to their versions. For articles, keep
first seen, asserted publication and observed content update separate; flag
conflicting publication dates and retain their original values/provenance. An
undated recovered article can be described as newly observed without inventing a
publication date. W10 renames and W23 changed-evidence routing must use the same
identity policy.

---

## W10. A changed headline changes the URL, and the same article becomes a new item

**Status:** open — needs a session (identity and migration)

**Finding.** Several publishers put the headline slug in the URL beside a stable
article id. A retitle changes the URL; the external id is the URL, so the
retitled article is a new identity: selected, title-gated, body-fetched and
alerted again, and counted twice in a report.

**Evidence.** Stored identities sharing one source and article id under different
URLs, 2026-09-15: Handelsblatt 146 articles with 259 extra URLs, FAZ 44 / 87,
WiWo 69 / 84, Spiegel 53 / 53, SZ 12 / 39, t3n 9 / 9. None has been body-gated
twice yet. In the 36-hour re-crawl, 31 of the 93 missing hints were such renames,
e.g. WiWo `…/bayer-tochter-fordert-genehmigung-des-roundup-vergleichs/100254574.html`
→ `…/bayer-tochter-monsanto-fordert-genehmigung-von-vergleich/100254574.html`.

**Candidate action.** A per-source canonical id pattern in the source entry
(`/(\d{6,})\.html$` for Handelsblatt and WiWo, `-(\d{9})\.html$` for FAZ, `li\.\d+`
for SZ, `-a-<uuid>` for Spiegel) that becomes the identity; for full-text sources,
`rel=canonical` from the fetched page is a second signal.

**Side effects to check.** The external id keys `raw_item`, `body_fetch`,
`assessment` joins, `alert_decision`, the title-gate JSONL logs and frozen bundles.
Changing it needs a migration that merges existing duplicates, and a decision on
which URL a report links. Sources without an id in the URL keep URL identity.

**Lower-risk launch option to discuss.** First calculate verified publisher ids
as an additional logical grouping key and produce a duplicate audit, preserving
existing raw row ids, URLs and frozen bundles. Readers can then group known
aliases consistently without immediately rewriting every historical reference.
This still needs deliberate integration with selection, alerts and reporting;
adding a column alone fixes none of them. Keep source namespaces, reject ambiguous
patterns, and test distinct articles as well as renamed copies. Defer a destructive
merge until duplicate effects and all affected references have been reconciled.

---

## W11. One item the alert model cannot answer blocks every alert

**Status:** done 2026-09-15, not deployed

**Finding.** `_decide` raised `AlertGateError` after two unusable replies or failed
calls. `run_alert_gate` stopped before advancing the watermark and before emailing
already-decided alerts, `cmd_alert_gate` did not catch that error, and every later
run retried the same item first. `parse_reply` rejects `potential_alert=false` with
any summary text, a plausible repeated model habit. `openai_caller` also reports
any `BadRequestError` — for example a content filter on one article — as a
configuration error, which stopped the run as well. Not observed: production had
made 2 alert decisions.

**Action taken** in [alert_gate.py](src/alert_gate.py#L300):
- Two replies in the wrong shape give a flagged alert: `potential_alert` true, a
  fixed Chinese "could not decide, please read the article" summary, and
  `fail_open` and the error in the payload. The reviewer is a human; recall is the
  point.
- A call that fails outright leaves that item undecided and moves on. The
  watermark does not advance, so the next run retries only that item.
- A configuration error counts as one item's refusal once any call in the run has
  been answered; two before any answer stop the run.
- Three fail-open or undecided items in a row stop the run: an outage or a broken
  model, not an item.
- Decided alerts are emailed in every case. `alert-gate` exits 2 when stopped and
  1 when an item was left undecided, so health sees a persistent failure.

Tests: five new tests in `tests/test_alert_gate.py`, from
`test_an_unusable_reply_becomes_a_flagged_alert_and_later_items_are_decided` to
`test_three_failures_in_a_row_stop_the_run`.

**Remaining.** The body and title gates still stop a whole run on a per-item
`BadRequestError`, since they share `openai_caller`. The body gate orders newest
first, so a poison item there blocks the older backlog, not new items. Not
measured.

---

## W12. The report can link an item its own ledger says it did not use

**Status:** done 2026-09-15 as a verification warning, not deployed

**Finding.** Rendering resolves any frozen identity, and `verify-report` checked
only that a URL is frozen and whether the item was opened. `schema.REPORTED` was
documented as what the renderer and verifier use to decide what may be cited;
neither used it.

**Evidence.** The 2026-09-05..11 report links item 23095 in its watchlist
("8月背景说明") while its ledger row is `archive_no_week_update`, "no bearing on an
open issue". Items 4706 and 30190 are linked in the coverage section as unreadable
titles, which is legitimate.

**Action taken.** [verify.py](src/report_agent/verify.py#L179) warns when the
title, verdict, a section or the watchlist links an identity whose treatment is
not in `REPORTED`; links in `scope_note` and `coverage_markdown` are exempt. The
result carries `cited_but_not_reported`. A warning, not an error, like
`cited_but_unread`.

**Open.** Whether a cited history item should instead get `background_only`.
Decisions are written before the write step, so that needs the write step to
report its citations back, or a rule applied at render.

---

## W13. An unsupported story could still be written up as a finding

**Status:** done 2026-09-15 (the `use` mapping), not deployed; headline correction open

**Finding.** `_build_register` downgraded `use` only for `background_only`. A
story that deep read or the challenge marked `insufficient_evidence` kept `use:
main`, and the write step gives every `main` issue its own section, while the
ledger calls the same story unsupported. Not seen in the first cycle: all four
sections were `main` and reportable.

**Action taken.** `insufficient_evidence` with `use: main` becomes
`conditional_watch` ([assess.py:494](src/report_agent/assess.py#L494)), so it can
appear in the watchlist and never as a section. Test:
`test_an_unsupported_story_is_never_written_up_as_a_main_finding`.

**Open.** The challenge step can correct `what_happened`, `why_it_matters`,
`scope_limits` and `status`, but not `headline`, which becomes the register's
`status` line and label.

---

## W14. The carry-forward step ignored its configuration

**Status:** done 2026-09-15, not deployed

**Finding.** `_carry_forward` hardcoded `effort="medium"` and `max_tool_calls=40`,
so `report_agent.steps.carry_forward` in `config.json` had no effect.

**Action taken.** It takes the configured effort and
`report_agent.carry_forward_tool_calls` (40, now in `config.json`), like the other
agentic steps ([assess.py:293](src/report_agent/assess.py#L293)). Test:
`test_carry_forward_takes_its_effort_and_budget_from_config`.

---

## W15. The weekly export is cumulative, so bundles and backups grow without bound

**Status:** open — needs a session, together with W1

**Finding.** `export-window` freezes every identity any gate ever judged, every
DIP document ever fetched, every Safety Gate record matching the client and every
DSA row — not the window's.
The ledger must account for all of them, so "identities accounted for" and the
bundle grow every week. Each daily backup's state archive holds all of
`data/reports/`, so backup size grows with the sum of all bundles: roughly the
square of the number of weeks.

**Evidence.** The first bundle is 17.9 MB: `source-evidence.html` 5.7 MB,
`stopped_items.json` 4.2 MB, `evidence.json` 2.6 MB, `dip_documents.json` 2.4 MB.
Its 1,004 ledger identities are mostly rule rows: `retain_gate_stop` 538,
`archive_no_week_update` 222, `background_not_reported` 120. The body gate adds
about 30 decisions a day, so each weekly bundle grows by roughly 200 identities.
`backup.warn_total_mb` is 1,024.

**Candidate actions.**
- Bound the export by a lookback — identities first fetched within N weeks — plus
  every id the previous register references, so carry-forward keeps its evidence.
- Keep derived pages out of the state archive (`source-evidence.html` is rebuilt by
  `report`), or archive only the latest bundle per client.

**Side effects to check.** Carry-forward searches stopped items to recover a
continuing story (CLAUDE.md); a lookback caps how far back that search reaches, so
choose N from how old the recovered items actually were. Complete accounting stays
exact within the bounded export. A frozen bundle must stay re-renderable.

---

## W16. A source that silently stops yielding is not detected

**Status:** open — needs a session

**Finding (corrected by the follow-up audit).** W8's three sources stored nothing
new for three to five days without a source-run failure or learned-yield warning:
- `status` is `ok` whenever something was found, even if all of it was already
  stored (VerkehrsRundschau: "ok, 8 found, 0 stored").
- `health/analyze.py` needs 7 comparable days and warns on a `zero_streak` only
  when the baseline median is at least 2. A source that breaks during its learning
  week learns a median of 0 and never warns: DVZ (25, 9, 3, 0, …),
  haendlerbund.de (81, 3, 0, …), VerkehrsRundschau (251, 33, 10, 0, …). The first
  day is the 30-day backfill, which the median ignores anyway.

**Existing detection, verified 2026-09-15.** `data/health/latest.json`, generated
04:15:37 UTC, already had status `warning`: the independent canaries found DVZ
**0/9** recent URLs and etailment **4/10** in SQLite. Thus the original statement
"no warning anywhere" was incorrect. All 32 configured article sources were
still learning their baseline. `health/canaries.json` covers only four sources:
DVZ, etailment, OHN and LOGISTIK HEUTE; Händlerbund and VerkehrsRundschau have no
independent check. The daily marker's exit 0 means the observer ran, not that its
findings were healthy. Receipt of the warning by the operator was not verified.

**Candidate actions.**
- Extend the existing independent endpoint canaries to the missing priority
  sources. Keep endpoint comparison independent of the production discovery
  parser: reusing that parser can reproduce its omissions and falsely agree.
  Use a grace period and verified publisher identities to reduce fresh-item and
  renamed-URL noise. A wider production-parser pass remains useful for diagnosis
  and recovery, but is not the sole coverage check.
- Exclude a source's backfill run from its baseline, and give sources with an
  expected cadence a floor the median cannot learn away.
- Treat disabled sources as disabled, and expose structured-collector failures
  too (W24). Track an unresolved incident until recovery is demonstrated rather
  than interpreting a successful scheduled task as its resolution.

**Side effects to check.** The check crawls: it belongs on the laptop after the
daily run, under the lock, and costs one discovery pass. Renamed URLs (W10) look
absent until W10 is fixed, so compare by article id where one exists.

---

## W17. Customer news reaches the alert model only with an alert word

**Status:** open — question for the customer, not a bug

**Finding.** `with_triggers` offers an item to the alert model only when it names
an own brand or matches an `alert_types` term. The alert prompt says a sensitive
event affecting a key customer counts, but customer-brand news without one of
those words never reaches it.

**Evidence.** Of 266 news items admitted by the gates to 2026-09-15, 52 carry a
trigger; 43 name a customer brand and carry none. Most are newsletter material by
the taxonomy's own design ("Bol erhöht den Vorsprung auf Amazon"). Some are
measures on parcels that a carrier may want same-day: "Österreichs Paketsteuer:
7,40 Euro Aufschlag", "Frankreich bittet Ultra-Fast-Fashion zur Kasse", "Temu-Zoll
wirkt". The customer's list has no customs, de-minimis, parcel tax, GPSR or DSA
alert types; `alert_taxonomy.json` records that the profile keeps those topics, not
that they alert.

**Question for the customer.** Should customs, de-minimis and parcel-tax measures
affecting a key customer or the parcel sector be alert types?

**blocking issues** Zeit blocked because we frequented it too often. this can potentially happen with others too. temporarely: removed zeit from source, removed intraday runs of laptop... until we fix the politeness...