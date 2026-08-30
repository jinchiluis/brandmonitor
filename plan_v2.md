# Brand & Reputation Monitor — Product Plan v2

Working document. Supersedes [new_product_plan.md](new_product_plan.md), which stays
on disk as the v1 record. Status: **pre-build — no application code written yet.**

---

## 0. What changed since v1, and why

v1 assumed one question: *what is being said about us?* It explicitly ruled out
legislation ("**Explicitly not in scope:** legislation / government tracking. The
L1–L7 risk layer model from germany_risk_monitor does not carry over").

New information from the customer side says there are **two** questions, and they
are not the same question:

1. **What laws affect us** — ESG, supply chain, product and environmental regulation.
2. **What is being written in the media that could turn into a crisis.**

They are *parallel*: different sources, different filters, different clocks, and
different failure modes. They share a datastore, a report, and a delivery channel —
almost nothing upstream of that.

| | **Track A — regulatory** | **Track B — reputation** |
|---|---|---|
| Question | what will apply to us | what is being said about us |
| Direction | forward-looking, months of warning | reactive, hours to days |
| Keys on | **company attributes** — product categories, materials, sourcing countries, EU role, turnover | **entity names** — brands, aliases, sellers, executives |
| Sources | Bundestag DIP, EUR-Lex/CELLAR, EP procedure DB — public APIs | ~50 sites + social + manual |
| Fetch difficulty | trivial: JSON/SPARQL APIs, no paywall, no proxy, no Playwright | the entire v1 fetch stack |
| Cost driver | LLM over legislative PDFs | crawl breadth + manual labor (D1) |
| Cadence fit | biweekly is generous | biweekly is too slow (see §7) |
| Fails by | missing a law → compliance surprise | missing a thread → crisis |
| Buyer | compliance / legal | PR / marketing |

**The consequence that matters:** a law reaches a company by *attribute*, never by
name. No amount of brand-term search finds CSRD, LkSG, GPSR, the Battery Regulation
or ESPR. D3 ("entity-based filtering, not topic-based") is correct for Track B and
false for Track A. This is the one place where v1's architecture genuinely does not
stretch, and it is why the answer is two pipelines rather than more sources in one.

**Correction carried into v2:** v1 §4 lists `c:/apps/germany_risk_monitor` as the
source repo and dismisses `c:/apps/NewsCrawler` as "pre-GRM Southeast Asia project".
There is no `germany_risk_monitor` directory on this machine. The GRM working copy
**is** `c:/apps/NewsCrawler` — remote `github.com/jinchiluis/germany_risk_monitor`,
HEAD `6a86115`, the exact commit already vendored under `vendor/newscrawler/`. All
GRM paths below refer to `c:/apps/NewsCrawler`. CLAUDE.md and
[vendor/PROVENANCE.md](vendor/PROVENANCE.md) need the same fix.

---

## 1. The product

A Chinese manufacturer/supplier whose brands are sold in Germany needs to know what
is coming at it from two directions: the regulatory pipeline in Berlin and Brussels,
and the German media conversation about its brands.

| | |
|---|---|
| Customer | Chinese company, supplier of consumer brands sold in Germany |
| Use case | **(A)** regulatory + ESG exposure monitoring **(B)** reputation / PR monitoring |
| Sources | A: Bundestag DIP, EUR-Lex, EP procedure DB · B: ~50 customer-identified sites + social |
| Cadence | Biweekly report, both tracks · daily alert tier on Track B (§7) |
| Deliverable language | Chinese |
| Competition | Traditional agencies, €2–5k/mo, slow; compliance consultancies bill by the hour |
| Our edge | One-man operation, automated pipeline, Doubao for Chinese output, transparent pass-through costs, **and a regulatory pipeline already built and running in another product** |

Track A is the stronger commercial position of the two. Reputation monitoring has
incumbents; a Chinese-language, German-regulation early-warning feed aimed at
Chinese exporters has very few, and the pipeline for it already exists.

---

## 2. Decisions

D1–D8 carry over from v1 unchanged and are not restated here — see
[new_product_plan.md](new_product_plan.md) §2. D3 is amended by D10.

| # | Decision | Notes |
|---|---|---|
| D9 | **Two tracks, one product, one report.** Track A (regulatory) and Track B (reputation) are separate pipelines over separate sources, joined only at store and report time | Not two products, not one pipeline with more connectors |
| D10 | **D3 applies to Track B only.** Track A filters by company *attributes*, not entity names | Requires a second client config artifact — `profile.json`, §5.3 |
| D11 | **Vendor GRM's gov *fetch* layer only.** No prompts, no pipeline agents, no LLM code | §4.1 — the whole answer to "what do we vendor" |
| D12 | **Track A persists in SQLite from day one.** `track_history.py` is not vendored; its record shape is the migration reference | D7 applies to both tracks; no JSON DB enters this repo |
| D13 | **L1 and L2 come with the crawler. L3–L7 are rebuilt, not vendored** | They are deterministic and already inside the fetch modules — §4.1 |
| D14 | **Track A does not depend on the residential IP.** Public APIs, no paywall, no Playwright | Makes it the DR-safe track. Does **not** relax "VPS never runs a scheduled cycle" (§5.6) |
| D15 | **Vendored code is our code. Purity is not preserved.** Edit `vendor/` directly and freely | We will never re-vendor from upstream, so a byte-identical copy buys nothing. §4.1 — **read it before inventing a workaround** |
| D16 | **One live `.env`, on the laptop.** The VPS holds `SCRAPE_SERVER_TOKEN` only | No sync, no drift, no encryption. DR credentials already exist on the VPS under rewriter |
| D17 | **Our logger module is `src/logger.py`**, not `src/log.py` | Both vendored trees import `src.logger`. Picking the name costs nothing and edits nothing. Closes the open question in PROVENANCE |

---

## 3. Scope

### In scope

**Track A — regulatory**
- Bundestag legislative procedures (DIP API, `f.vorgangstyp=Gesetzgebung`)
- EU legislative proposals (EUR-Lex/CELLAR via SPARQL) + procedure lifecycle events (EP procedure DB)
- Legislative-stage tracking with change detection across cycles
- Attribute-based relevance filter against a per-client company profile
- Per item: what it changes, who it binds, key dates, thresholds, business impact, recommended action

**Track B — reputation** (unchanged from v1 §3)
- ~50 customer-identified websites; brand-term search as a second recall path
- YouTube (Data API v3, incl. comments), Reddit
- FB / IG / TikTok / X via BrightData datasets **or** manual labor per platform (D1, D2)
- Entity dossier per client

**Joint**
- One biweekly Chinese-language report with two parts (§6)
- The A×B cross-link (§5.5) — the part neither track produces alone

### Out of scope
- **Compliance advice.** The product says *"this is coming, here is when, here is what
  it binds"*. It does not say *"here is what you must do to be compliant"*. That line
  is legal advice with liability attached — see §9 Q1
- Regulators below federal/EU level (Länder, Landesbehörden), standards bodies, court rulings
- Real-time monitoring (§7 daily alert tier is the deliberate exception, Track B only)
- Customer-facing self-serve portal
- Languages beyond DE source / ZH output

### Undecided — see §9
- Whether the "50 websites" are media outlets or e-commerce/review sites (v1's open
  question, unchanged, still gating)
- Whether Track A and Track B are one subscription or two

---

## 4. Reuse map

One repo contributes: **germany_risk_monitor** at `c:/apps/NewsCrawler`. v1's take
list stands. What changes is that the "drop entirely" list loses most of its
contents — the gov tree is now the seed of Track A.

| Area | Take | Track |
|---|---|---|
| Discovery | `crawler.py`, `crawler_google_feeds.py`, `crawler_html_utils.py`, `parallel_crawler.py`, `source_loader.py` | B — already vendored |
| Fetch + extract | `scraper.py`, `scraper_fetch_html.py`, `crawler_playwright.py`, `paywall/` | B — already vendored |
| **Gov fetch** | **`bundestag.py`, `europarl.py`, `europarl_epdb.py`, `pdf_utils.py`** | **A — to vendor, §4.1** |
| Agents | `assessment_agent.py`, `embedding_agent.py`, `llm_client.py`, `llm_cost_calculator.py` | both |
| Ops | `logger.py`, `config.py`, `mocker.py`, `health_check.py`, watermark pattern | both |
| Report | `word_report.py` as docx *mechanics* only | both |

### Reference, not code — read and rebuild

| Thing | Where | Why not vendored |
|---|---|---|
| L3 risk taxonomy A1–A11 + AxBy | `src/agents/system_prompts/layer3_risk_prompts.json` | Prompt content, tuned to GRM's client sector. **Structurally an excellent fit** — A5 `ESG与供应链法规`, A9 `环境与产品监管`, A2 trade defense / market access, A6 labor. Retarget the prompt blocks to consumer goods; keep the shape |
| L4–L7 layer definitions | `gov_profiler_agent.py`, `system_prompts/layer7_enterprise.md` | L7 already emits `enterprise_impact` + `action_current/6_12m/pre_effect` — which is §6 A4, the thing buyers pay for. Rebuild against our schema |
| Gov PDF summary format (7 sections) | `system_prompts/summary_gov.md`, `gov.md` | 核心判断 / 核心义务 / 关键政策变化 / 关键时间节点 / 适用范围 / 主要影响 / 具体数字门槛. Already Chinese, already the right sections. Copy the section list, rewrite the prompt |
| News→Vorgang linking funnel | `main_report_de.py`, `system_prompts/linker.md` | 3-stage: L3 overlap → embedding similarity → LLM linker. This is §5.5 |
| Vorgang record shape | `track_history.py` (48 lines) | `history[]`, `_known_event_ids`, `_known_pdf_urls`, `_epdb_process_id`. It is a JSON DB; D12 says SQLite. Read it to write the migration |
| FAISS dynamic prompt banks | `src/agents/memory_banks/` | `dynamic_prompts` is `false` in GRM's own `config.json`, commented *"k are too flat, could potentially miss important tags"*. Skip until there is a reason |

### Drop entirely
`main_gov_de.py` (338), `pipeline_agent_bundestag.py` (334),
`pipeline_agent_europarl.py` (438), `main_report_de.py` — orchestration written
against GRM's JSON DB and prompt set. ~1,100 lines we rewrite against SQLite.

### 4.1 What to vendor from `src/crawler_gov/` — and what not

**Yes: the fetch layer, and only the fetch layer.** Four files, 834 lines, zero LLM
calls, zero prompt files, dependencies `requests` + `pymupdf`.

```
bundestag.py       276  DIP API: fetch_vorgaenge, fetch_vorgangsposition,
                        fetch_pdf_urls_for_vorgang, download_pdf, crawl_vorgaenge
europarl.py        311  SPARQL over publications.europa.eu + CELLAR PDF resolution:
                        fetch_proposals, cellar_uuid, get_pdf_urls, crawl_proposals,
                        check_adoption
europarl_epdb.py   210  EP procedure API: celex_to_process_id, get_procedure_events,
                        check_procedure_updates
pdf_utils.py        37  pymupdf text extraction, max_pages / max_chars
```

**No:** `track_history.py` (D12), `states/` (live JSON DB, watermarks and downloaded
PDFs — same rule as `vendor/newscrawler/states/`), `__pycache__/`, and everything
under `src/agents/`.

#### The ownership rule (D15) — read this before inventing a workaround

Once copied, these files are **ours**. This project will never re-vendor from
upstream, so a byte-identical copy buys nothing and costs plenty. Edit them
**directly**:

- **Do:** change a line, delete dead code, rename a parameter, fix a bug
- **Don't:** monkey-patch module globals from an adapter to avoid an edit
- **Don't:** re-implement a vendored function so the original can stay untouched
- **Don't:** add a shim whose only job is preserving a `diff` against GRM

Every one of those rescues is more complex than the edit it avoids, and each one
protects an option — clean `diff`, clean re-copy — that we have decided not to use.
If a change is needed, make the change. **YAGNI applies to process, not only to code.**

This was nearly gotten wrong during planning: the DIP key was going to be injected by
reassigning `bundestag.API_KEY` and `bundestag.HEADERS` at runtime, and the
Wahlperiode fix by re-implementing `fetch_vorgaenge`'s cursor loop in our own module
and leaving the original dead in the tree. Both are worse than editing two lines.
Anyone reading `bundestag.py` would have seen a hardcoded key with no hint it was
overridden three files away.

`vendor/` therefore means **"originated elsewhere, now ours"** — not
"upstream-managed, do not touch". CLAUDE.md already says exactly this of
`vendor/newscrawler/` (*"Heavily patched fork — treat it as our code"*); D15 extends
it to the gov tree and makes it the standing rule for anything vendored later.

The cost, recorded once so it is not rediscovered as a surprise: a DIP or CELLAR
breakage now gets fixed **twice**, once per repo. Both repos are ours, so that fix
was a manual copy either way — losing the diff made it less *visible*, never less
automatic.

**But "just the scraping scripts" understates what comes along.** Three pieces of
*domain logic* live inside those four files, and they are the most valuable
non-obvious content in the tree:

- `SIGNIFICANT_BT_EVENTS` (`bundestag.py:216`) and `SIGNIFICANT_EVENTS`
  (`europarl_epdb.py:58`) — which procedural events count as a real change
- `_STATUS_MAP` (`europarl_epdb.py:31`) — EP event → legislative stage. **This is
  L2**, deterministic, and it is what turns a document feed into a warning system:
  knowing a proposal moved from committee to second reading is the signal
- `find_new_events` / `is_significant` in both modules — the change detection that
  makes delta cycles cheap

Hence D13: **L1 (jurisdiction, authority level) and L2 (legislative stage) arrive
free with the vendored fetch layer, without touching a prompt.** Only L3–L7 are LLM
work.

#### Changes to make on arrival — two edits and a deletion

Under D15 these are ordinary edits to our own files, not "integration debt".

1. **DIP API key → `.env`.** `bundestag.py:25`, commented *"public DIP key, valid
   until end of May 2027"* — it expires in ~9 months and is shared with every other
   product using it. Two lines change: `API_KEY`, and the module-level `HEADERS`
   computed from it. Becomes `BUNDESTAG_DIP_API_KEY`, key #26; under D16 there is one
   copy, on the laptop, so nothing can drift. Register our own key rather than
   inheriting GRM's.
2. **Delete `"f.wahlperiode": 21`** from the params dict in `fetch_vorgaenge`
   (`bundestag.py:57`). Keep `f.vorgangstyp` and `f.aktualisiert.start`. Justified by
   measurement below.
3. **Delete the dead code** — `_PROJECT_ROOT` and `DEFAULT_OUT` in both modules
   (nothing we take reads them; `download_pdf(url, dest)` requires its destination),
   and the uncalled `matches_keyword` / `matches_sachgebiet` helpers.
4. **`src.logger` and `src.config.CRAWLER_VERBOSE`** — no edit. D17 names our module
   `src/logger.py` and has `src/config.py` export `CRAWLER_VERBOSE`, and the four gov
   files then import cleanly as they stand.

Worth noting: the four gov files **do not import each other** — only stdlib,
`requests`, and those two `src.*` names. The gov tree therefore has none of the
`src.crawler_news.*` self-referential path problem PROVENANCE flags for the news
vendor. It is a materially cleaner tree to take.

#### Measured — the crawl is not a firehose

Live DIP counts for a 14-day window (2026-08-16 → 2026-08-30):

| Query | `numFound` |
|---|---|
| `f.vorgangstyp=Gesetzgebung` + `f.wahlperiode=21` | **58** — what GRM's query returns |
| `f.vorgangstyp=Gesetzgebung`, any Wahlperiode | **75** — 17 more, silently dropped today |
| no type filter at all | **1277** — `f.vorgangstyp` is doing real work, keep it |

**58–75 procedures per cycle.** A batched title assessment over 75 items on Doubao is
fractions of a cent, and the crawl is ~75 × 0.3s plus request time — about a minute.
The relevance filter's job is therefore **precision, not volume reduction**, and with
no cost pressure it should lean *inclusive*: let borderline items through, because
the expensive stage is PDF summarization downstream, not the titles.

**Why delete the Wahlperiode filter (edit 2).** The 17 extra items are not new
legislation — they break down as WP20 ×7, WP19 ×1, WP18 ×3, WP17 ×3, and one each
from WP12, WP11, WP10. The WP20 entries are bills from the previous Bundestag
(*Gesetz zur Modernisierung des Schiedsverfahrensrechts* and similar) which died at
end of term under Diskontinuität and are now receiving archival updates; the older
ones are decades-old records getting metadata touches. So the filter *is* removing
real noise today. The trade is still one-sided:

| | Hardcoded `21` | No Wahlperiode filter |
|---|---|---|
| Noise | none | +17 items/cycle |
| Cost of that noise | — | ~€0.001 in title assessment |
| Failure mode | **silent zero after the next election — no error, forever** | cannot ever silently return zero |

Fail-safe beats precise when the precision is worth a tenth of a cent and the failure
is invisible: an empty result set renders as "no legislative activity this period",
indistinguishable from a genuinely quiet fortnight, and would run for months
unnoticed. The noise self-clears anyway — a 2003 Transplantationsgesetz record will
not survive a relevance filter keyed to consumer-goods ESG exposure. Keep the
zero-yield health check regardless (§9).

**GRM has the same latent bug** and will hit the same silent zero after the next
federal election. Worth fixing there too.

#### What vendoring does not give us

`crawl_vorgaenge(since)` pulls every updated procedure and filters nothing —
`matches_keyword` and `matches_sachgebiet` sit in the file uncalled. All filtering is
LLM-side, in the title assessor, which is exactly the part we are not taking. So the
vendored crawler supplies ~100% of the fetch and **0% of the relevance logic**, and
that is the real work item of Track A. It is cheap to run and unbudgeted to build —
do not plan Track A as "mostly vendored". Sachgebiet prefiltering is worth testing as
a first cut, though German legal titles are opaque enough (*Gesetz zur Änderung des
Kreislaufwirtschaftsgesetzes* is packaging law and matches no packaging keyword) that
the abandoned helpers are probably evidence it was tried and dropped.

**Mechanics:** `git archive origin/master` from GRM into `vendor/govcrawler/` — still
the right extraction method, because it leaves the local GRM checkout untouched and
records a commit hash rather than a copy date. Confirm first whether `origin/master`
has moved since `6a86115`. Then add a `govcrawler` section to
[vendor/PROVENANCE.md](vendor/PROVENANCE.md) — but under D15 that file is now an
**origin record and change log**, not a purity contract: the `Modified since copy`
field is meaningless and goes away, replaced by a list of what we changed and why.

---

## 5. Architecture

### 5.1 Source registry with heterogeneous connectors

v1 §5.1 stands. Track A adds three connectors to the same registry — same record
normalization, same `collection_method` provenance:

```
api:bundestag_dip    # DIP v1, cursor-paged, legislative procedures + events + PDFs
api:eurlex_sparql    # publications.europa.eu SPARQL + CELLAR PDF resolution
api:ep_procedure     # data.europarl.europa.eu/api/v2 — procedure lifecycle events
```

They are the easiest connectors in the product: authenticated JSON and SPARQL, no
paywall, no Playwright, no proxy, polite fixed delays already in the code.

### 5.2 Datastore

v1 §5.2 stands — SQLite, WAL, FTS5, `VACUUM INTO` snapshots, rolling ~30d retention.
Track A adds its own tables rather than sharing the mention table: a legislative
procedure is a long-lived object with a history, not an append-only mention.

```
procedure          one row per Vorgang / EU proposal — jurisdiction (L1), stage (L2),
                   celex, dip id, ep process id, first_seen, last_changed
procedure_event    the history[] timeline — every procedural event, significance flag
procedure_doc      PDFs: url, sha, local path, extracted text, summary
procedure_assess   L3–L7 output, versioned per assessment run
mention            Track B records (v1 schema)
mention_procedure  the A×B link (§5.5) with stage, score and method
```

`_known_event_ids` and `_known_pdf_urls` — which the vendored `find_new_events`
requires as inputs — become queries over `procedure_event` and `procedure_doc`
rather than fields in a JSON blob. That is the whole of D12's migration work.

**`src/gov.py` — the adapter, and what it is *not* for.** One thin module owns the
translation between the vendored crawler's GRM-shaped dicts and our schema: supplying
the known-id sets from SQLite, passing `data/` paths as `dest`, and normalizing
records on the way in. That is genuine data-model translation and it earns its place.
It is **not** a place to route around editing the vendored code (D15) — if the
crawler needs to behave differently, change the crawler.

### 5.3 Client config — dossier **and** profile

v1 §5.3's `entities.json` stands for Track B. Track A needs a second artifact,
because it filters by what the company *is*, not what it is *called*:

```
clients/<slug>/
  client.json      contract, cadence, tracks enabled, delivery
  entities.json    brands, aliases, transliterations, products, execs, sellers,
                   negative keywords                        → Track B  (v1 §5.3)
  profile.json     product categories, materials, battery / packaging / WEEE /
                   textile exposure, sourcing countries, EU legal role (manufacturer /
                   importer / marketplace seller / authorized representative),
                   EU turnover band, employee count, customs codes
                                                            → Track A  (NEW)
  sources.json     Track B site list + connector per source
```

`profile.json` is to Track A what the entity dossier is to Track B: the highest-
leverage config artifact and the main source of both false positives and false
negatives. The legal-role field carries disproportionate weight — GPSR
responsible-person duties, EPR registration, battery and packaging obligations
attach to the role, not to the brand, and a supplier selling through a German
importer has a materially different exposure than one placing goods on the market
itself. Turnover and headcount bands decide CSRD/CSDDD phase-in applicability
outright.

### 5.4 Two pipelines

```
TRACK A — regulatory                      TRACK B — reputation
1. Crawl   DIP + EUR-Lex + EP API         1. Discover  per-connector, 2 recall paths
2. Sort    new vs. known (SQLite)         2. Triage    entity match, keyword + LLM
3. Filter  attribute relevance vs.        3. Extract   fetch ladder → trafilatura
           profile.json  (LLM, batched)   4. Ingest    manual records join here
4. Fetch   PDFs, extract text             5. Dedup     embedding, cross-source/cycle
5. Assess  L3–L7 + 7-section summary      6. Analyze   sentiment, reach × severity,
6. Track   lifecycle events → L1/L2                    theme clustering
           recomputed deterministically   7. Store     persist, advance watermark
7. Store   persist, advance watermark
                          \                      /
                           →  8. LINK  (§5.5)  ←
                                     ↓
                            9. REPORT  one docx, two parts (§6)
```

Separate watermarks per track — they move at different speeds, and a failure in one
must not stall the other. Threading model unchanged from v1: `ThreadPoolExecutor`,
HTTP serialized per domain, LLM free-running.

Track A's step 3 is where the cost is decided and where all the new prompt work
goes. Steps 1, 2, 4 and 6 are largely the vendored code plus SQLite.

### 5.5 The join — the part worth selling

GRM already links news to legislative procedures through a 3-stage funnel: L3
category overlap → embedding similarity → LLM linker confirm. Ported here, it
answers a question neither track asks alone:

> *A regulation you are exposed to is being covered negatively in German media right
> now — and your brand is one search away from being the example in the article.*

That is the actual crisis early-warning for a Chinese consumer-goods supplier. Not
"someone wrote about us" (too late) and not "a law is coming" (too abstract), but
"the enforcement story is heating up and we are the obvious illustration". It is
also the answer to a customer who asks why they should buy both halves.

Cheap to build — stage 1 is a category-overlap query, stage 2 reuses the embedding
agent already in the take list, and only stage 3 costs an LLM call, on a small
candidate set.

### 5.6 Deployment

v1 §5.5 stands in full and is not restated: laptop primary, VPS standby, the
scheduled-run prohibition enforced in `run.py` by env var, pull-not-push deploy,
Tailscale, the Task Scheduler session-0 trap, rolling snapshots.

One property worth recording: **Track A has no residential-IP dependency** (D14).
Public APIs, no paywall, no Playwright, no proxy. It would run correctly on the VPS.
That does **not** loosen the prohibition — two hosts writing one DB is still a
diverged store and duplicate spend — but it does mean a DR run of Track A is a
genuine one-command fallback, and that a future Track-A-only customer is not
laptop-bound.

---

## 6. Report structure

One document, two parts, one executive summary spanning both.

```
0. 摘要 — executive summary across both tracks

PART A — 法规与合规 (regulatory)
A1. New and changed items this period, by stage
A2. Per item: 核心判断 · 核心义务 · 关键政策变化 · 关键时间节点 · 主要影响 · 数字门槛
A3. Timeline — what takes effect when, next 24 months
A4. 企业影响与建议行动  (L7)

PART B — 舆情 (reputation)                          [v1 §6, unchanged]
B1. Volume + sentiment vs. previous period
B2. Notable mentions, ranked by reach × severity
B3. Emerging narrative themes
B4. Competitor comparison (if in scope)
B5. 建议行动

CROSS. 监管—舆情交叉预警  (§5.5)
Appendix: full mention list, full procedure list, collection_method provenance
```

The 7-section summary format and the L7 action fields already exist in GRM's prompts
and are already Chinese — the section list carries over even though the prompt text
is rewritten. `word_report.py` (646 lines) remains the docx *mechanics* reference;
its legislation-shaped structure is now partly relevant rather than not at all.

---

## 7. Daily alert tier

Unchanged from v1 §7 — and now explicitly **Track B only**. Legislation does not
spike; media does. Running Track A daily would spend LLM budget re-confirming that
nothing moved.

The asymmetry is an argument for the tier rather than against it: the biweekly
cadence that suits Track A is visibly wrong for Track B, which is easier to sell
than "we would like to charge you more for the same thing, faster".

---

## 8. Cost model

Per client, per biweekly cycle. Track B lines are v1 §8 unchanged.

| Line | Estimate | Notes |
|---|---|---|
| **A** — API crawl | ~€0 | DIP, SPARQL, EP API all free. **Measured: ~75 procedures, ~1 min wall clock** (§4.1) |
| **A** — relevance filter LLM | **~€0.01** | Doubao, one batched pass over **75 titles** (§4.1) |
| **A** — PDF summarization LLM | **the Track A cost line** | Long documents; GRM uses `max_chars=60000` per PDF. Bounded by *relevant* items per cycle (tens, not hundreds) |
| **A** — L3–L7 assessment | small | Only on new/changed items; deterministic L1/L2 are free |
| **A subtotal** | **€10–30/cycle** | Crawl and filter are now measured and negligible — **the entire uncertainty is the PDF stage**. Delta model means a quiet cycle costs near zero |
| **B** — all lines | **€20–60/cycle** | v1 §8 |
| Link stage (§5.5) | negligible | Stage 3 LLM on a small candidate set |
| Contabo VPS | €5/mo | Backup + heartbeat |
| Laptop power | €3–5/mo | Hardware owned |
| BrightData — socials only | €0–300/mo | Track B only |
| **Manual labor (D1)** | few hundred €/mo, pass-through | Track B only, zero margin, disclosed |

Track A adds real LLM spend but no bandwidth, no proxy, no manual labor and no
paywall subscriptions — the cheapest possible thing to bolt onto an existing
pipeline. The top two lines are now measured; the €10–30 total still rests entirely
on an unmeasured PDF stage, since it depends on how many of the ~75 procedures pass
the relevance filter and how long their documents are. GRM's
`llm_cost_calculator.py` running totals from its own gov cycles are the fastest way
to replace that with a real number, and it should happen before quoting.

---

## 9. Risks and open questions

### Open questions for the customer

1. **Monitoring or advice?** "Here is what is coming and when" is what this pipeline
   can defend. "Here is what you must do to comply" is legal advice with liability
   attached. Draw the line in the SOW before the first report sets an expectation.
2. **What is their legal entity position in the EU** — manufacturer, importer,
   marketplace seller, or selling through a German importer who holds the
   obligations? Most product-regulation duties attach to that role. Half of
   `profile.json` and much of Track A's precision depend on this answer.
3. **Product and material inventory at category level** — batteries, electronics,
   textiles, packaging types, sourcing countries. Not SKUs.
4. **EU turnover band and headcount.** Decides CSRD/CSDDD phase-in applicability
   outright. Likely commercially sensitive; a band is enough.
5. **One deliverable or two?** PR and compliance are usually different departments
   with different budgets and different tolerance for a 14-day lag. May be two
   subscriptions rather than one report.
6. *(carried from v1, still gating)* **What are the 50 websites** — media outlets, or
   e-commerce/review sites? Materially changes Track B's build.
7. *(carried from v1)* **Which social platforms**, posts only or comments too,
   keyword search or named-account monitoring. Name them in the SOW.
8. *(carried from v1)* Full brand / product / entity list; competitor list if in scope.

### Risks

v1 §9's risk table carries over in full. New with Track A:

| Risk | Mitigation |
|---|---|
| **The relevance filter is unbuilt and unavoidable.** GRM's gov crawl is unfiltered by design; its assessor prompt does all the work and is tuned to a different sector. There is no vendored shortcut | Budget it as the main new build item. Volume is *not* the problem (§4.1: ~75/cycle) — precision is. Validate against a hand-labeled set of ~50 known-relevant and known-irrelevant Vorgänge before trusting a cycle |
| **False negatives are invisible.** A missed law surfaces as a compliance failure months later, with our report as the evidence that we said nothing | Recall test on known-applicable regulation (LkSG, GPSR, Battery, ESPR, EPR/VerpackG, CSDDD): the pilot must find all of them. Disclose the source scope in every report — federal + EU only |
| **Advice liability** (Q1 above) | Contract language; report wording stays descriptive; no "you must" |
| **A silently empty crawl reads as a quiet fortnight**, not as a fault — the `f.wahlperiode: 21` class of bug | Removed at source by §4.1 edit 2, but the class remains: zero-yield health check on procedures-per-cycle, per track |
| **DIP API key expires end of May 2027**, shared across products | Move to `.env` (§4.1), calendar reminder, health check on 401 |
| **Both products share live credentials** (CLAUDE.md → Secrets) — now including a second product hitting the same DIP key | Rotation plan; register our own DIP key rather than inheriting GRM's |
| **Bugs in the shared crawler get fixed twice** (D15) — GRM and brandmonitor now hold independent copies of the DIP/CELLAR fetch code | Accepted cost, recorded in §4.1. Both repos are ours, so the fix was a manual copy either way. When fixing one, check the other — starting with the Wahlperiode bug, which GRM still has |
| **Two tracks, one-man operation** — roughly doubles the surface that can break silently | Per-track health checks and per-track watermarks; a stalled Track A must not look like a quiet legislative period |

---

## 10. Next steps

1. **Vendor `src/crawler_gov/` per §4.1** — four files into `vendor/govcrawler/` via
   `git archive origin/master`, then make the two edits and the deletion immediately,
   so the tree is ours from the first commit rather than becoming ours by accident. Plus: PROVENANCE `govcrawler` section with the `Modified since copy`
   field dropped, `BUNDESTAG_DIP_API_KEY` in `.env`, and the `c:/apps/NewsCrawler`
   path fix and D15 wording in CLAUDE.md and PROVENANCE.
2. Get answers to §9 Q1, Q2 and Q6 — these gate the build.
3. Collect `profile.json` inputs (Q2–Q4) and the entity list.
4. **Pilot both tracks in one cycle.** Track A's pilot is cheap and fast — the
   vendored crawler plus a hand-written filter over one 14-day window will show
   within a day whether the relevance problem is tractable. Run it against the known
   regulation list from the recall-test risk above.
5. Scaffold the repo: `run.py` with per-track stage flags, `migrations/001_*.sql`
   covering both schemas, and `src/logger.py` + `src/config.py` (exporting
   `CRAWLER_VERBOSE`) per D17 — both vendored trees import cleanly against those two
   names and neither needs editing for it.
6. Stand up the laptop (v1 §5.5 checklist, unchanged).
