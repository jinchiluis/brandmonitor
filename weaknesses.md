# Weaknesses

Findings from reviewing the pipeline's output. Each entry lists what was measured,
a candidate action, and what that action could break. **Nothing here is decided.**
Go through the entries before implementing any of them; an entry that turns into
work moves to `todo.md`, and an entry rejected on review stays here with its reason.

Status values: `open` (not reviewed), `accepted` (move to todo.md), `rejected`
(reason recorded), `done`.

Sources for the numbers: the production database on 2026-09-14, the bundle
`data/reports/jt-express-2026-09-05_2026-09-11`, and
`data/title_gate/jt-express/2026-09-11.jsonl`. One report cycle and a few days of
gating are a thin sample; the per-source figures in W7 especially.

---

## W1. Undated items are offered to triage every week, indefinitely

**Status:** open

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

**Candidate action.** Offer an undated item to triage only if its **first**
version was fetched inside the window.

**Side effects to check.**
- `surfaced_in_window` uses the *latest* version's `fetched_at`. Filtering on it
  as-is would let a restamp (W2) bring an old item back. First-seen has to be
  computed separately.
- An undated page whose content really changed later would no longer be offered.
  Carry-forward search still reaches it; decide whether that is enough.
- Accounting stays complete: an undated row outside the pool already gets the
  rule treatment `undated_background` ([assess.py:630-631](src/report_agent/assess.py#L630-L631)).
- The first cycle after the change drops the backlog from triage all at once.
  Compare the ledger with the previous bundle to see what that removes.

---

## W2. A `lastmod` restamp writes a new version of a title-only item

**Status:** open

**Finding.** A title-only item is re-stored as a new version whenever
`sha256(url|title|published_at)` changes ([src/collect.py:222-224](src/collect.py#L222-L224),
[collect.py:356-358](src/collect.py#L356-L358)). On a plain sitemap the only date
is `<lastmod>`, which CLAUDE.md defines as a change signal only. When a CMS
regenerates its sitemap, the timestamp moves, the hash changes, and every URL gets
another row with the same title.

Full-text sources are not affected: a changed discovery hint queues a body
re-check and never writes a version by itself ([collect.py:349-354](src/collect.py#L349-L354)).
The title gate is not affected: it reads only items whose first version the day's
run stored ([src/selector.py:310-314](src/selector.py#L310-L314)).

**Evidence.** 682 stored rows differ from the previous version only in a
`lastmod` date: BVL 579 (one regeneration on 2026-09-13 02:17), ZEIT 99, WELT 4.

**Where it still does harm.**
- The export takes the latest version before the cutoff, so a restamp flips an
  old item's `surfaced_in_window` to true.
- Row count and backup size grow on every regeneration.

**Candidate action.** Leave a `lastmod`-sourced date out of the hash, or skip
storing when the only difference is a `lastmod` date.

**Side effects to check.**
- **Changing the hash formula re-hashes every existing title-only item.** On the
  next run, every stored hash would mismatch and every title-only source would
  write a new version of every URL in its window. This needs either a comparison
  that recomputes old hashes the same way, or a one-off migration.
- `published_at` on the stored row would keep the first `lastmod` seen. It is
  never shown to a customer, but check that nothing sorts or windows on it.
- A title-only source with a news sitemap (`<news:publication_date>`) must keep
  versioning on a real date change. Only `date_source == "lastmod"` should be
  exempt.
- Existing guards: `test_changed_title_becomes_a_new_version` and
  `test_repeated_runs_do_not_ratchet_versions` in `tests/test_collect.py`;
  `test_lastmod_churn_rechecks_without_content_versions` in `tests/test_bodies.py`
  (full-text path). Add a regression test for "title-only, same title, new
  lastmod, no new row". DIP and EP already hash without their paperwork stamps
  (`test_hash_ignores_documentation_stamps_but_not_content`), which is the
  precedent.

---

## W3. Extra versions in general: mostly useful, one class unexplained

**Status:** open (audit only, no action proposed)

**Evidence.** 24,940 items, 29,819 rows, so 4,879 extra versions. Each version
was compared with the previous one:

| Change from previous version | Rows | Reading |
|---|---:|---|
| Title changed, with or without the date | ~2,980 | Mostly body enrichment: the sitemap stub (bare URL, often no title) becomes headline, text and page date |
| Same title and date | 249 | Body content changed |
| Date only, not `lastmod` | 970 | **Not examined.** Largest: WELT 441, t3n 102, SZ 85, ZEIT 68, etailment 66, WiWo 65 |
| Date only, `lastmod` | 682 | Noise, see W2 |

**Why strict URL dedup (first sighting wins) is not the fix.** It would freeze
the enrichment stub, lose real corrections and retitles, lose DIP/EP procedure
steps that arrive under the same id, and break the bundle's "latest version before
the cutoff" traceability.

**To do before deciding anything.** Sample the 970 date-only changes: are they a
page date correcting a discovery date (useful), or another restamp pattern?

---

## W4. BVL: high cost, no output

**Status:** done 2026-09-14 (config only)

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

**Remaining effects.**
- The 621 stored items stay in the database. BVL items that already passed a
  gate will still appear in later bundles and, until W1 is fixed, in triage.
- The health observer records BVL as `zero` every day. Its baseline was still
  learning on 2026-09-14 (3 of 7 comparable days). Nearly all of its days stored
  zero, so the median should be 0 and no `zero_streak` warning should fire.
  **Verify once the baseline is ready.**
- Deleting the stored rows would be a separate, confirmed step. Not planned.

---

## W5. Frontpage discovery stores topic and listing pages as articles

**Status:** open

**Finding.** A frontpage source keeps every link it finds, and `allowed_dirs`
seeds which section pages are scraped rather than filtering results. On BPEX the
seed sections are stored as items themselves: `themen-und-positionen/zoll`,
`/postgesetz`, `/verkehr-und-umwelt`, `/innenstadtlogistik`,
`/arbeit-und-soziales`, plus `kep-branche/zahlen-und-fakten` and an item titled
just "Meldung". The body gate marked all 16 gated BPEX items relevant, because a
topic page on postal law *is* on topic.

**Evidence.** 6 of the 15 undated BPEX items in the 2026-09-05..11 triage were
topic or listing pages. The BVL `/blog/` index got through the same way.

**Candidate action.** Exclude those exact pages, not whole sections.

**Side effects to check.**
- `excluded_dirs` is a prefix rule. Excluding `themen-und-positionen` would also
  drop any article under it, and may interfere with seeding from that section.
  Check what the frontpage collector does with an excluded seed.
- An exact-URL mechanism may fit better (the blacklist file, or a new
  `excluded_urls`). Check whether the blacklist is applied at collection.
- Related: the body gate judges topic, not whether a page is an article. The hub
  gate only starts after 20 stored bodies, so a small source like BPEX may never
  reach it.

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

**Status:** open (re-measure after about four weekly cycles before deciding)

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

**Reading, not a decision.**
- *Expensive, nothing yet:* LOGISTIK HEUTE, haendlerbund.de, VerkehrsRundschau.
  LOGISTIK HEUTE had brand hits historically, so one cycle is not enough.
- *Cheap, nothing:* BVDW, DSLV, BGL. Dead weight rather than a burden.
- *Cheap insurance:* mainstream outlets. Under 1% of their items reach an LLM,
  and a large brand story would appear there.
- *Slow by nature:* DIP, EP procedures and the regulators. A procedure moves over
  months, so weekly yield is the wrong measure.

**Side effects to keep in mind.** The CLAUDE.md test for a source is whether it
produces something a weekly report or an alert would carry. Measure over enough
cycles that one quiet week doesn't remove a source a later story needs.
