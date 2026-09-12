# Report and assessment plan

Status: **built, in `src/report_agent/`**, as `run.py export-window / assess /
report / verify-report`. This document is kept as the design record: it explains
why the stack has the shape it has, from the worked example in
`data/reports/experiment-2026-09-05_11/` that produced it. Where the two
disagree, the code is what runs; §10 records what was decided while building.

The operator-facing summary is in [CLAUDE.md](CLAUDE.md), "The weekly report
stack", and the durable design reasoning is in
[docs/selection_and_assessment.md](docs/selection_and_assessment.md), "Weekly
assessment and report".

`src/assess.py` is untouched and unused. It was the stub for a per-item
assessment; §2.1 below is the argument that the unit is an issue rather than an
item, so the stack that replaced it lives in `src/report_agent/assess.py`.

## 1. What the experiment was

An external model produced a full weekly Chinese report for jt-express over
2026-09-05..11 from the stored database, by hand, and left the scaffolding
behind. Three stages, and the middle one is not code:

| Stage | File | Output | Judgment |
|---|---|---|---|
| Freeze | `build_evidence.py` | `evidence.json`, `stopped_items.json`, `gate_census.json`, `manifest.json` | none — deterministic export |
| Decide | *nowhere* | 18 issues, 41 item verdicts, 36 bodies read | entirely in the model's context |
| Render | `build_review_bundle.py`, `verify_bundle.py` | ledger, coverage, 5 HTML pages, assertions | none — deterministic |

The editorial decisions live as Python literals: the `stories` list, the
`weekly_decisions` dict, the `full_read` / `section_read` ID sets, and one-off
branches such as `if i==24617`. Re-running the script re-renders them; it does
not re-derive them.

So the artefact is not a report generator. It is an evidence-freezing harness
plus a rendering and audit harness, with an analyst-shaped hole between them.
That hole is what §4 has to fill.

### Honest scale of the review

The ledger accounts for 1,004 identities, but 908 of those rows got their
treatment from an `if`/`else` template:

| Rows | Treatment | Source of the string |
|---:|---|---|
| 537 | `retain_gate_stop` | template plus the stored gate reason |
| 204 | `archive_no_week_update` | template |
| 120 | `background_not_reported` | template, DSA aggregates |
| 47 | `historical_pattern_reference` | template, older Safety Gate |
| 96 | 17 hand-written treatments | actual reading |

Measured reading depth: 31 full bodies, 5 targeted sections, everything else
title-level. The English review states this; the rendered ledger's density does
not, and a client reading that page would over-estimate it. Any production
version must make depth measurable rather than asserted — see §5.

Two further limits. The baseline week is reconstructed: collection began
2026-09-09, so 09-05..08 is backfill, and the experiment proves nothing about
week-over-week detection. And `coverage.json` and `unavailable_title_evidence.json`
are consumed by the render script but produced by nothing in the bundle; its
README's re-render instruction works only because those two files happen to sit
on disk.

## 2. Findings that change the build

1. **Relevance is not treatment.** The gates answer "is this relevant". The
   report needs a second, different answer: `report`, `merge`, `background_only`,
   `carry_forward`, `insufficient_evidence`, `omit_for_priority`. `merge` and
   `carry_forward` are structurally impossible for any per-item scorer, which is
   the same conclusion §1.1 reached from the cadence side.
2. **The unit of report state is an issue, not an article.** `issue-register.json`
   carries `baseline` IDs, `current` IDs, a status, and the evidence that would
   trigger the next update. The next cycle starts from dated status rather than
   last week's prose.
3. **An open issue must search material the gate rejected.** Item 24617, a
   2026-09-07 port strike, was stopped by the body gate as non-parcel-specific
   and directly continues retained August item 24618. It reached the report only
   through manual recovery. That is a change to the funnel, not a prompt tweak,
   and it is only possible because the issue register exists.
4. **Titles are not enough to survey with.** The product-safety sweep arrived
   inside a Commission digest titled "Daily News". A headline-only pass drops it.
5. **Duplicate sources inflate a report.** Four groups covering nine articles
   describe four developments: Hermes 2, Wish 2, EUDI 3, packaging relief 2.
   Unmerged, that is five spurious entries in a single week.
6. **`body_status = ok` is not evidence.** A teaser with subscriber furniture, a
   podcast episode description, and two Verbraucherzentrale pages holding
   identical unrelated sidebar text all passed extraction.
7. **A source may report nothing.** DSA aggregates earned no metric: submission
   days are not event days, Shein filed zero in the covered window, and some
   TikTok action-date ranges run past the cutoff. The report must be allowed to
   say so rather than forced to carry a chart.
8. **Complete accounting is the auditable contract.** Every candidate and every
   stopped item gets a row. A report claiming eight findings without saying what
   happened to the other 996 identities cannot be checked.

## 3. Target shape

The same three stages, with the deterministic halves promoted into `src/` and the
middle stage actually built:

```text
run.py export-window  --client --since --until   →  frozen bundle (read-only)
run.py assess         --bundle                   →  decisions + issue register
run.py report         --bundle                   →  zh report, ledger, coverage
run.py verify-report  --bundle                   →  verification.json, exit 1 on error
```

Stage 1 reuses the selector and gate logic from `src/` rather than restating it.
`build_evidence.py` hardcodes the client slug, the parliamentary source tuple and
the window literals; duplicating selection rules is exactly the drift the design
rules warn about.

Stage 1 keeps the experiment's good habits: `mode=ro` URI, `BEGIN` then
`rollback`, and a manifest recording snapshot time, window bounds, row counts,
the stated eligibility policy, and SHA-256 of every input file. This is the
operational form of the §1.1 argument that assessment is completely reversible —
it turns "re-run under a new schema" into a checkable claim.

## 4. Stage 2: how the LLM step is organised

**Multistep, with state on disk rather than a preserved conversation.**

The evidence is already frozen on a filesystem, so the assessor should navigate
it with read tools instead of carrying it. Three lifetimes, three mechanisms:

| Scope | Mechanism | Why |
|---|---|---|
| Within one story's deep read | live context | the reasoning is the point |
| Between steps | structured artefacts on disk | re-runnable, diffable, verifiable |
| Between weeks | the issue register only | a transcript is not a traceable record |

Passing a growing transcript between steps fails four project rules at once:
decisions must be traceable to prompt and profile version; the stage must be
re-runnable additively under the `(raw_item_id, client_slug, prompt_version,
profile_version)` key; stage 3 can only verify output that is keyed rows; and one
story's framing must not bleed into the next.

### The steps

| Step | Input | Shape | Output |
|---|---|---|---|
| 1. Carry-forward | last register, plus this week's candidates **and stopped items** | retrieval and judgment | matches per open issue |
| 2. Triage | all candidate titles, dates, gate reasons | batched fixed calls, cheap tier | shortlist and `omit` reasons |
| 3. Cluster | shortlist rows only | one call over compact rows | story groups, `merge` decisions |
| 4. Deep read | one story at a time | **agent loop with tools** | evidence excerpts, status, scope limits |
| 5. Challenge | draft story and its evidence | adversarial pass | corrected claims |
| 6. Write | issue register only | one call, never the corpus | zh report sections |

Steps 2, 3 and 6 are fixed calls, the same pattern as the existing gates. Steps
1, 4 and 5 are genuinely agentic: they decide what to read next based on what
they just read. The port recovery is the canonical case — the September item
mentions a continuing dispute, resolvable only by searching back for the August
one, including among items the gate stopped.

Step 5 is where most of the experiment's quality came from and is the easiest
thing to lose when automating. It caught the Wish duplicate, the EUDI triple
count, DHL's total rather than incremental capacity, Myflexbox's municipalities
rather than lockers, and the AliExpress alert published 2026-09-11 for a removal
on 08-25.

### Tool surface

Read-only, local, no network:

```text
get_item(raw_item_id)     frozen record, payload, dates, provenance
get_body(raw_item_id)     stored body text
search_titles(query)      candidates and stopped items
search_bodies(query)      stored bodies
open_issues()             last cycle's register
```

Five tools, mirroring the discipline that keeps the vendored crawler's surface
small. The assessor reads the bundle and nothing else; no network call at any
point in this stage.

### Model

Not decided here, and not the gates' model. The gates are a cheap screen in front
of the assessment; the assessment is the product, and it is the stage that reads
bodies, challenges its own first interpretation and writes customer-facing
Chinese. It gets a capable model.

What carries over from the gates is only the convention: a dated model snapshot
pinned per step in `config.json`, with the measurement that justified it in its
`_comment`, so the model cannot change underneath the stage.

## 5. Instrument depth instead of asserting it

The experiment infers `review_depth` from hardcoded ID sets, so the field is a
claim. In production it should be **derived from the assessor's logged tool
calls**: `get_body(24617)` was called, so that row's depth is
`full_stored_body`; only `get_item` was called, so `title_or_overview`.

That makes the ledger's most important column measured rather than asserted, and
it is a property the manual version could not have had.

## 6. Invariants stage 3 enforces

Verification is deterministic and knows nothing about the model. It is what makes
fabrication structurally detectable rather than something a reviewer must catch:

- every decision references a `raw_item_id` present in the frozen export;
- every identity in the export carries exactly one decision — bijection, no gaps;
- every external URL in the report appears in the frozen evidence;
- every `fetched_at` is before the window cutoff;
- census arithmetic reconciles: gated = passed + stopped + post-cutoff;
- no `lastmod` date reaches a customer-facing field;
- no broken local anchors, no `�`, no raw markdown links in rendered HTML.

`verify_bundle.py` already does all of this, but with the week's counts as
literals (`==1004`, `==464`, `==538`). The checks generalise; the constants must
come from the manifest.

## 7. What stays deterministic

Do not pay a model to say `archive_no_week_update` 204 times. The bulk ledger
treatments, the coverage table, the census arithmetic, the hashes and the HTML
rendering stay rules. The assessor emits decisions for the shortlist and the
issues; the renderer joins them against the frozen export and fails loudly on a
gap.

## 8. Open decisions

- Model and reasoning effort per step, and cost for a full cycle. Unmeasured.
- Whether the issue register is a table — `report` is still empty — plus a frozen
  JSON export, or a file per cycle. Traceability argues for the table.
- Bound on the carry-forward search. Re-reading every stopped item each week
  reopens the gate's rejects indefinitely; scope it by open issue, not by corpus.
- Where the human review gate sits. For the pilot, mandatory review before any
  client sees a report, with the reviewer marking which items changed a decision,
  which were excessive, and which expected developments were absent.
- Chinese output quality assurance. The experiment's zh report was model-written
  and reviewed in one pass; that is the pilot arrangement, not the target.
- Whether `review_depth` and the treatment vocabulary appear in the customer
  deliverable or only in the internal ledger.

## 9. Relation to the existing plan

This does not change [mvp_plan.md](mvp_plan.md). It fills `todo.md §1.2` and
sharpens §1.3:

1. Agree the treatment vocabulary and issue-register schema (§2, §4). This is the
   assessment output schema §1.2 asks for.
2. Build stage 1 as `run.py export-window`, reusing `src/` selection logic.
3. Build stage 3's renderer and verifier against the frozen bundle, with
   constants read from the manifest.
4. Build stage 2 last, against a schema the other two already enforce.
5. Re-run the 2026-09-05..11 window and diff against the manual bundle. It is a
   hand-made reference answer for exactly one week; use it while it is fresh.
6. Then a second fixed week, per the review's own recommendation, with a human
   marking misses and excess.

The second experiment is worth more than the first. The first showed the process
can produce a coherent, inspectable draft; it measures no recall, no legal
accuracy, no normal workload and no live multi-week cycle.

## 10. Decided while building, 2026-09-12

Against §8's open list:

- **Model:** `gpt-5.6-sol` for every step, reasoning effort per step in
  `config.json`. §4 expected a cheap tier for triage; it is the same model at
  `low` for now, because the cheap-tier saving is not worth a second pinned
  snapshot before anything is measured. It has **no dated snapshot**, so the
  convention this project follows everywhere else cannot be honoured here —
  `assessment-manifest.json` records the model and the per-step prompt hash on
  every run so an upstream change is visible after the fact.
- **Register:** a file per cycle inside the bundle, found by scanning earlier
  bundles for the latest one that closed before this window opened. §8 argued
  for a table; a dated artefact that diffs is the better fit while the schema is
  still moving, and moving it into SQLite is a change to one reader.
- **Carry-forward bound:** scoped by open issue, as §8 asked. Step 1 searches
  for each issue's own `next` trigger and records the queries it ran, so a
  negative result is auditable. The rejected corpus is never swept.
- **Cost:** still unmeasured as a per-cycle figure. The first full run over
  2026-09-05..11 was 73 model calls, 80 tool calls, 381k input and 58k output
  tokens, about 15 minutes wall clock. No price is pinned for `sol`.
- **Human review gate and Chinese QA:** unchanged, still §8's pilot answer.
- **`review_depth` and the treatment vocabulary** appear in the internal ledger
  only. The customer report carries scope limits in prose instead.

Two things the build changed that §1-§7 did not anticipate:

- **An unreadable title is a first-class identity.** The first render failed
  because the report cited item 4706 — the FAZ page whose body never arrived —
  to say a title was seen and could not be read. That is a true and useful
  sentence, so the export now shapes those rows like every other identity and
  the ledger accounts for them. 1,004 rows, matching the hand-made cycle.
- **The challenge step's scope limits replace the draft's, not extend them.**
  Appending produced near-duplicate caveats in the customer's own report.

The automated runs reproduced the experiment's frozen counts exactly — 464
candidates, 538 stopped, 210 post-cutoff, 1,004 identities — and the challenge
step independently caught the same class of error the hand-made cycle did,
including DHL's total rather than incremental sorting capacity and the swimming
seat's 25 August removal published on 11 September. That is a coincidence of a
few runs over one week, not a measurement; §1.3 of `todo.md` lists what would be.

Measured on the 2026-09-05..11 window, 15 minutes wall clock:

| | |
|---|---:|
| offered to triage / kept | 74 / 27 |
| stories clustered / read / reportable | 11 / 11 / 10 |
| challenge problems, of which corrections | 12 / 6 |
| ledger rows written by a rule / by the assessor | 930 / 74 |
| bodies opened in full / seen only in a search / title only | 18 / 280 / 706 |
| model calls / tool calls | 72 / 73 |

**Run-to-run variance is real and unmeasured.** Two runs over the same frozen
week, same prompts, same model, gave 13 and 11 stories, and the writing step
turned those into 10 numbered sections in one and 4 sections plus a 6-row
watchlist in the other. The findings were substantially the same; how much of
each got a full section was not. Before a customer sees this weekly, either the
section/watchlist split needs a rule rather than the writer's judgment, or the
variance needs measuring across several runs. This is the clearest argument for
the human review gate §8 already asks for.
