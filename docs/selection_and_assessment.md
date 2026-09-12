# Selection and assessment design

This document records the durable reasoning behind client matching and the planned
assessment funnel. Active implementation work belongs in `todo.md`; customer terms
belong in `clients/<slug>/`.

## Collection tiers

Collection is source-specific and assessment is client-specific. Raw source material
is stored before applying a client profile.

| Tier | Storage policy | Why |
|---|---|---|
| Trade press and associations | Full text | A brand or rule can appear only in the body; the main trade sources carry most measured signal. |
| Regulators | Full text | Regulatory relevance is topical and cannot reliably be judged from a headline. |
| Mainstream publishers | Title and URL first; body only after a match | Volume is high, signal is low, and bulk use of paid accounts creates publisher and terms-of-service risk. |
| Parliamentary procedures (DIP, EP) | The API record, with a body composed from it | The record already says what the procedure is and what happened; the documents behind a DIP step are fetched later, only for the procedures a client's gate kept. |

Full text everywhere was rejected because paid-account bulk fetching is risky, not
because storage is expensive. Title-only everywhere was rejected because it makes
recall impossible to measure and leaves reports without quotable evidence.

The source-level policy is configured with `content_mode`. Path restrictions are
configured with `allowed_dirs` and `excluded_dirs`; their exact semantics are in
`CLAUDE.md`.

## Deterministic selection

The matcher is client-driven. Rules live in `clients/jt-express/profile.json`, not
in `src/selector.py`, and every assessment must retain the profile version. The
same selector runs over news and regulatory bodies; why regulatory keeps it is in
"Body gate" below.

- Titles and URL labels use a high-recall OR: one matching brand, topic, or eligible
  keyword is sufficient.
- Bodies require one brand or narrow topic, or at least two broad keywords. In a
  long body a single broad word is usually accidental.
- A rule with `scope: body` never matches a title or URL. In a body it only
  completes a pair with a regular keyword; two body-only rules together select
  nothing. *Verordnung* plus *Bußgeld* with no market word is a law page, and that
  pattern selected Wettbewerbszentrale category pages and little else.
- A trailing `*` permits suffixes for German morphology. `pattern` exists only for
  cases terms cannot express, such as J&T spacing, CJK text, and ambiguous UPS.
- Brand `role` (`own`, `competitor`, `customer`) is report/prompt metadata and does
  not change matching.

The body threshold was measured on the first 342 stored bodies:

| Variant | Selected | Brand/topic finds retained |
|---|---:|---:|
| Any single rule anywhere | 156 | 23 |
| Body needs at least two matches | 76 | 22 |
| One brand/topic, or at least two keywords | 77 | 23 |

The chosen rule removed obvious homonym and navigation noise without losing the
single-topic GPSR example.

Repeated on the backfilled corpus (2,374 full-text items, 2,336 bodies) with profile
`2026-09-10`: bodies select 1,148 items, titles and slugs alone 544, so 604 are found
only in the body, among them 77 brand and 35 topic finds. Some of those are company
overview and navigation pages, but the rest are exactly what a customer would miss:
France taxing ultra-fast fashion (Temu, Shein, AliExpress named only in the body),
new customs at the border, Amazon return fees, bpex on Postrecht reform, the
Händlerbund on US de-minimis shipping rules. Roughly half of all brand mentions in
trade press are not in the headline or slug. Fetch-on-match cannot recover that on
the mainstream tier, because it only fetches what the title already matched; keep
this figure next to any decision about paying for mainstream bodies.

The same run puts the full-text selection rate at 37 % (878 of 2,374), 734 of which
the pre-customer profile already selected. LOGISTIK HEUTE is 401 of the 878; 72 of
its bodies carry a footer tag list (*Lagerhallen, Logistikimmobilien, Logistik
Newsletter, KEP Dienste, … Supply Chain Management*) that alone accounts for 54
selections. The rest are genuine keyword pairs in article text.

### Hub-page gate

A stored body with no page date, on a source whose articles state one, is skipped
before matching. The expectation is derived from the stored rows, not configured:
a source qualifies when at least 80 % of its stored bodies carry a page date over
at least 20 bodies (`PAGE_DATE_SHARE`, `PAGE_DATE_MIN_BODIES` in
`src/selector.py`). Regulators, bevh and Verbraucherzentrale sit at 0 % because
their templates state no structured date, so the rule never applies to them.

Measured 2026-09-11 on the full corpus after the config narrowing: seven sources
qualify (LOGISTIK HEUTE, VerkehrsRundschau, etailment, ohn, Händlerbund, e-commerce
Magazin, t3n) and 24 rows are skipped - the eight VerkehrsRundschau `/nachrichten`
section indexes (identical 885-character furniture), the seven etailment `/magazin`
section indexes, the Händlerbund press hub, two e-commerce Magazin rows that share
one identical body, four LOGISTIK HEUTE editorial newsletters and one ohn page. The
last five are the known cost: real pages on templates that state no date. Skipped
rows are counted as `hub_suspects` in the selection result and reported by the CLI;
nothing is deleted or rewritten, so the rule is reversible by lowering the
thresholds.

Why this signal and not the others measured: `og:type` is unreliable (WordPress
archives say "article", VerkehrsRundschau articles say "website"); the `<article>`
count is unreliable (Drupal article pages carry 11-17); body length catches only the
big hubs and date-stamp density only LOGISTIK HEUTE. "No structured date on a
template that has one" is free once date extraction runs and separated every hub
in the sample. Config narrowing (`allowed_dirs`, `excluded_dirs`) does the positive
work of choosing sections; the gate only catches what shares a section with real
articles.

## Customer keyword mapping

The customer's five monitoring rows are intent, not literal search syntax.

| Customer request | Representation |
|---|---|
| Competitors: DHL, Hermes, UPS, FedEx, DPD, GLS, GoFo, iMile | Brand rules with `role: competitor` |
| Key customers: Shein, TikTok Shop, Temu, AliExpress | Brand rules with `role: customer` |
| Logistics AND policy | Sector terms plus body-scoped policy vocabulary |
| Own-brand incidents | J&T brand selection; incident terms classify alerts later |
| Compliance risks | Assessment taxonomy, not selection vocabulary |

Incident words are intentionally absent from deterministic selection. Measured
alone, words such as *Unfall*, *Brand*, *Streik*, *Krise* and *Strafe* selected 333
headlines and none co-occurred with a configured brand. A real carrier incident is
already selected by its carrier or sector term; the incident vocabulary belongs in
`clients/jt-express/alert_taxonomy.json`.

The customer's logistics-and-policy expression is enforced in bodies. Broad German
policy terms are body-only because on headlines they never co-occur with a sector
word (1 of 12,449 mainstream titles) and alone produced 58 unrelated ones. Terms
were chosen by per-term body hit rate: *Richtlinie, Regelung, Vorschrift,
Verordnung, Bußgeld, Behörde, Urteil, Gewerkschaft, Mindestlohn, Tarif*. Dropped
from the customer's list: *Recht* (footers), *Arbeit*, *Politik* (nav menus),
*Amt*, *Union* (Europäische Union), *officer* (Chief Executive Officer), and the
English column, which is dead on German text.

**The sector side is parcel-specific on purpose**: *Zusteller, Paketdienst, Kurier*
next to the existing *Paket, KEP, Zoll, Onlinehandel, Marktplatz, China,
Lieferkette*. `logistik*` and `spedition*` are not in the profile at all. On the
first 249 bodies they looked harmless; on the full corpus `logistik*` added 585
items, 484 of them through *Logistik* pairing with *Lieferkette*, *Plattform* or
anything else, because trade press has the word in nearly every body and the AND
collapsed into "any second word". Even its policy pairs were LOGISTIK HEUTE on
volcanoes, Bundeswehr depots and labour law at Daimler Truck; on slugs it took 308
LOGISTIK HEUTE archive pages and 130 BVL blog posts straight to the expensive
stage. The narrowing drops port strikes, truck tolls and rail funding unless they
touch parcels, e-commerce, customs or China. That is an assumption about the
customer's intent and is listed under client questions in `todo.md`.

Profile `2026-09-10` against its predecessor on the backfilled corpus, nothing lost:

| Tier | Eligible | Old profile | New profile | Added |
|---|---:|---:|---:|---:|
| Mainstream title-only | 14,705 | 260 | 270 | 10 |
| All news sources | 17,079 | 994 | 1,148 | 154 |

The mainstream additions are DHL items (mostly welt.de stock notes; one real, the
Deutsche Post renaming). Of the 154, the brand hits are exactly the competitor row:
Brussels blocking FedEx's InPost takeover, Hermes QR returns, a VerkehrsRundschau
carrier comparison. 137 are body pairs, read one by one: about 55 real (the PPWR
packaging series, *Maxibrief 2027*, *Mindestlohnanstieg: Logistikverbände
befürchten Kostensprünge*, Myflexbox lockers, Kassenpflicht, EmpCo, the
Gewährleistungslabel series, Amazon Prime and returns rules, Batterieverordnung,
AI Act labelling, and a dozen Lkw-Fahrverbot items of marginal parcel relevance);
about 15 index or evergreen pages; about 65 noise, nearly all LOGISTIK HEUTE
pairing *Lieferkette* with a policy word. *Supply chain* is the next ambient word
after *Logistik* and the lever to pull if expensive-stage cost matters. Verdict:
the competitor and customer rows are pure gain; the policy row finds real
regulatory items at roughly 40 % precision and raised full-text volume by a fifth.

Those are historical measurements, not live dashboard values. Recalculate rather
than copying them into a report.

### Matched keywords in the report

Chinese monitoring reports usually print the matched keywords (命中关键词) beside an
item, and the customer's list reads like a query written for such a tool. The full
assessment will store a deterministic `matches` block: per rule, the matched text,
the field it was found in, and a count. It is computed when the full assessment
runs, over title and body, and frozen in that `assessment` row beside its
`profile_version`. Not at collection: matches depend on the client, `raw_item` is
shared, and a profile edit would leave stored hits stale without any sign. Not
from the body gate's `selector_reasons` either: those are rule names without the
matched word or field, and a title-only item has no body until after its gate.

Only what is matched exactly is printed:

| Printed | From |
|---|---|
| Brand, with its role's category label (`DHL` → 核心竞品动态监测) | brand rule and `role`, labels in `alert_taxonomy.json` |
| Topic (`Zollfreigrenze`, `GPSR`) | topic rule |
| A parcel-sector keyword next to a body-only policy keyword (*Kurier + Verordnung*) | the customer's row 3 in its own terms |
| 标题提及 for a title or slug match, 正文提及 for body only | the field |

The item's own category is the assessor's call: a customs item that names DHL in
its eighth paragraph is not competitor news. Other broad keywords stay internal.
On 2026-09-11, 130 of the 249 bodies the body gate kept or left unsure had matched
on broad keywords alone, and a tag such as *China + Plattform* explains why noise
got in rather than why the item matters.

Rows 4 and 5 are never printed as matched words. The assessor picks `alert_types`
from `alert_taxonomy.json`, and the report shows one only when the assessor did.
Measured over the same 724 body-gate decisions, 27 of 249 kept or unsure bodies and
41 of 475 dropped ones contain a row-4/5 word, so the words do not separate relevant
from irrelevant. Read one by one, the 27 kept hits are:

- three events rows 4 and 5 exist for: the FTC investigating Shein, €550M against
  AliExpress, Hermes's job cuts in a delivery restructuring;
- three pieces of marketplace enforcement a report would carry: the DSC's
  proceedings against eBay, the Commission's product-safety checks on e-commerce
  parcels, its foreign-subsidies probe of JD.com;
- seven studies: *Untersuchung* means a survey in 7 of its 13 hits (Swiss free
  shipping, a Hessen funding round, a carrier complaints comparison);
- three hypotheticals in merchant explainers ("kann mit einem Bußgeld von bis zu
  100.000 € geahndet werden", whether a strike counts as force majeure);
- the rest background mentions, the English word *brand* (*Brand Guardian*,
  "Brand Trademark"), or events outside the client's world: Google's DMA fines,
  port strikes, the taxi and bus trade.

German morphology rules out tuning that away. 8, 14 and 8 kept bodies carry
*Bußgeld*, *Sanktion* and *Strafe* only in another form: *Bußgelder*, *Sanktionen*,
*Strafen*, but also *Vertragsstrafe*, *Haftstrafe*, *Handelssanktionen*. Substring
matching finds all of those, and for *Ermittlung* also *Datenübermittlung* and
*Vermittlungsdienste*.

The hits do include the three real alerts, so they are kept in the payload for
audit rather than dropped: a body carrying an alert word that the assessor gave no
`alert_type` is the list to skim during the pilot.

## Assessment funnel

```text
title-only news           full-text news            regulators
      |                         |                        |
deterministic selector    deterministic selector    deterministic selector
      |                         |                        |
title gate                body gate, news       body gate, regulatory
      |                         |                        |
JSONL keep -> body queue        |               DIP relevant: documents
      |                         |               fetched, cut when read
fetch selected body             |                        |
      +-------------------------+------------------------+
                                |
                    full structured assessment
                                |
                       Chinese report/alert
```

The title gate sees only title-level evidence and decides whether a body is
fetched. Its JSONL `keep` records are the client-specific handoff; the existing
`body_fetch` table is only the shared operational queue and retry state. A kept
title-only item goes directly to the full assessor after fetching: the cheap title
LLM already approved it for this client, so the fetched body is enrichment rather
than input to a second relevance gate. Full-text news and regulatory bodies reach
their body gates through the deterministic body-aware selector. Regulatory records
skip the title gate because their relevance is topical, and use their own body-gate
prompt. A DIP procedure the regulatory gate judges relevant also brings the
documents behind its steps, cut to size when they are read ("Documents behind
parliamentary procedures" below).

Every stored assessment must identify the client, prompt version, profile version,
and source version. The report schema should be sketched before the final assessment
schema, because the report determines what the assessment must return.

## Title gate

`python run.py gate` (`src/title_gate.py`) asks a small model which title-only
candidates deserve a body fetch. `run_daily.bat` runs it after the news crawl.

- **Input.** Title-only candidates whose first version the day's news crawl stored.
  A restamp writes version 2 and is not new, so BVL's periodic `lastmod`
  regeneration does not re-offer its archive. Items with a stored body skip the gate.
  Each line carries the matched rules, the source, and the title - or the slug,
  since ZEIT ships almost no titles; publisher ID tails are stripped from slugs.
- **Prompt.** `src/prompts/title_gate.md` is shared by every client. The `prompt`
  section of the client profile supplies the description, what counts as relevant,
  and the false matches measured on that client's keywords. Brand names come from
  the brand rules by role. The prompt version is a hash of the rendered text.
- **Reply.** Numbers only, `0` for none. Anything else is retried once, and a batch
  that fails twice is kept whole: a drop is final for a title-only item.
- **Decisions.** Kept and dropped, with versions, in
  `data/title_gate/<client>/<date>.jsonl`, pruned after 30 days. Dropped items are
  recorded nowhere else.
- **Fetch handoff.** `python run.py fetch-bodies --kind news
  --title-gate-client <client>` reads the retained JSONL files and queues only their
  latest `keep` per source URL, even though those sources have
  `content_mode=title_only`. Once introduced, the existing `body_fetch` row owns
  retries, so pruning the decision log cannot strand a failed request. The daily
  script runs this as `title_bodies` immediately after the title gate. A successful
  route is direct input to the full assessor and skips the body gate.
- **No decision memory beyond the run.** A profile change applies to new items;
  `gate --all` re-gates the corpus deliberately, `gate --run N` redoes one day.
  The body queue remembers that an explicitly kept URL needs source material and
  the client route plus original selector reasons needed for delivery to the full
  assessor; the relevance decision itself remains in JSONL.

The model was chosen on the 247 title-only candidates stored on 2026-09-11,
hand-labelled under a strict rule: 23 keep, borderline items drop. Batches of 10,
three shuffles per variant, cost at 25 candidates a day:

| Model | Reasoning | Missed of 23, per run | Extras, per run | $/month |
|---|---|---|---|---:|
| GPT-5.4-mini | low | 1 / 0 / 0 | 1 / 0 / 1 | 0.07 |
| GPT-5.4-mini | none | 6 / 2 / 6 | 4 / 7 / 7 | 0.04 |
| GPT-5.6-luna | low | 0 / 0 / 0 | 3 / 2 / 1 | 0.02 |
| GPT-5.4-nano | low | 0 / 3 / 1 | 3 / 4 / 7 | 0.02 |
| GPT-5.4 | none | 1 / 1 / 2 | 2 / 2 / 3 | 0.12 |
| GPT-5.5 | none | 2 / 0 / 0 | 4 / 4 / 2 | 0.23 |
| Claude Haiku 4.5 | none | 2 / 1 / 4 | 9 / 9 / 6 | 0.05 |
| Claude Sonnet 5 | low | 4 / 0 / 3 | 1 / 2 / 2 | 0.14 |
| Claude Opus 5 | low | 0 / 0 / 0 | 6 / 5 / 3 | 0.44 |
| Doubao Seed 1.6 (251015) | low | 0 / 0 / 0 | 2 / 3 / 4 | ≈0.05 |
| Doubao Seed 1.6 (251015) | minimal | 8 / 4 / 3 | 4 / 20 / 4 | - |
| Doubao Seed 2.0 mini | low | 3 / 1 / 4 | 1 / 0 / 1 | ≈0.01 |
| Doubao Seed 2.0 mini | minimal | 0 / 2 / 4 | 1 / 2 / 3 | <0.01 |
| Doubao Seed 2.0 lite | low | 1 / 3 / 4 | 1 / 0 / 0 | ≈0.02 |
| Doubao Seed 2.0 pro | low | 2 / 3 / 5 | 0 / 0 / 0 | ≈0.10 |
| Doubao Seed 2.0 pro | minimal | 0 / 1 / 2 | 2 / 3 / 4 | ≈0.02 |

Doubao runs through Volcano Engine Ark's OpenAI-compatible endpoint with
`DOUBAO_API_KEY`. Its prices are the ≤32K-input tier in yuan, taken from launch
coverage because the official price page renders only in a browser, converted at
about 7.2 CNY/USD. Seed 1.6's tiers are quoted inconsistently, so its figure is an
estimate at ¥0.8 input and ¥8 output. Seed 2.1 turbo and pro are listed but were not
activated on the account, so they are untested.

- Reasoning off costs a small model its recall: mini without it dropped the Shein
  IPO, JD.com and the Deutsche Post renaming. Larger models without reasoning cost
  more than mini with it, because input dominates a short classification call, and
  still missed more.
- Cost decides nothing at this volume. `gpt-5.4-mini-2026-03-17` was chosen as the
  most precise option available as a dated snapshot, so the model cannot change
  underneath the gate. GPT-5.6-luna scored best but had no dated snapshot.
- Doubao Seed 2.0 with reasoning is stricter than the prompt asks: pro and lite
  dropped the Shein IPO and the DHL incendiary-parcel story, reading an IPO debut as
  a share-price note. Seed 1.6 with reasoning missed nothing and kept 2-4 extras per
  run, so it is the fallback if the gate ever has to move to a Chinese provider.
  With thinking off, Seed 1.6 twice answered with an explanation instead of numbers.
- The production command over the same corpus missed 0 of 23 and kept one extra,
  Alibaba's capital raise for AI.
- Borderline items - general e-commerce logistics, Alibaba, DSA duties for ChatGPT -
  flip between runs of the same model. None is an item a weekly report would lead
  with, so they are labelled drop rather than argued over.

## Body gate

`python run.py body-gate` (`src/body_gate.py`) asks a small model whether each
selected body is a signal for the client. `run_daily.bat` runs it after both
collections.

- **Input.** News and regulatory candidates that carry a stored body, have no
  body-gate decision yet, newest first, at most `limit` per tier and run
  (`config.json`). Full-text news and regulatory items come through the body-aware
  deterministic selector, and bodies carrying this client's successful title-gate
  route are excluded because they already have a cheap relevance decision. It reads
  every other selected body rather than only today's. An item is its source and
  external id: a restamp's version 2 is not re-offered.
- **Prompts.** `src/prompts/body_gate.md` shows a news body with the rules it
  matched; `src/prompts/body_gate_regulatory.md` shows a regulatory body without
  them and lists legal areas instead of business topics. The client's inputs come
  from the profile's `prompt` section: `relevant`, `false_matches` and
  `body_false_matches` for news, `regulatory_relevant` and
  `regulatory_false_matches` for regulatory. One body per call, the first
  `body_chars` characters.
- **Reply.** JSON with a reason and a verdict. An unusable reply is retried once;
  an item that fails twice is kept as unsure with `fail_open`. Three failed items in
  a row stop the tier and leave the rest for the next run.
- **Decisions.** In `assessment`: `relevant` 1 / NULL / 0 for relevant / unsure /
  irrelevant, prompt version `body_gate-<kind>-<hash>`, and the verdict, reason,
  model, selector reasons and tokens in the payload. Drops are stored too, so a
  wrong drop can be found with a query.
- **No memory beyond the decision.** A profile or prompt change applies to new
  items; `body-gate --regate` re-gates items decided under another version.

Only irrelevant stops an item; unsure goes on. The stage's case is quality and
measurability rather than cost: at about 25 selected bodies a day, the full
assessment over everything would cost tens of dollars a month, but a large model
asked for severity and actions will supply them for a *Lieferkette*-plus-*Verordnung*
page too. A keep/drop decision can be scored the way the title gate was; a full
assessment cannot. Relevance is therefore its own stage, not a field of the full
assessment call.

Measured 2026-09-11 with the title gate's model (gpt-5.4-mini, reasoning low), one
pass per sample, against 90 hand labels in
`clients/jt-express/labels/body_gate_2026-09-11.json`. Labels were set by reading
the full body before any model saw it; unsure means either verdict is acceptable.

| Sample | Labels rel / unsure / irr | Relevant kept | Irrelevant dropped | Kept |
|---|---|---:|---:|---:|
| 30 random selected bodies | 4 / 4 / 22 | 4 | 15 | 13 |
| 30 news bodies with a brand or topic match | 11 / 5 / 14 | 11 | 5 | 25 |
| 15 regulatory picked and 15 skipped by the selector | 1 / 0 / 29 | 1 | 27 | 3 |

- The selector's body precision is lower than the policy-pair figure above: 4 of 30
  random picks were relevant, and 1 of the 22 that matched on keywords alone. Brand
  and topic matches carry most of the signal.
- The false keeps fall into classes, most fixed outside the gate: evergreen legal
  Q&A for merchants and a shipper's own warehouse (candidate body false matches); a
  competitor's non-parcel divisions - DHL Freight and DHL Supply Chain appointments,
  because "a competitor as a business" is too broad for bodies; BPEX navigation and
  pagination pages, excluded by config since; and hedges answered unsure.
- A warehouse story matched UPS on "Set-ups". Scale-ups, Grown-ups, Back-ups and
  Set-ups - the only non-carrier hits in any stored title, slug or body - are
  excluded since profile `2026-09-11.2`.
- Regulatory needs its own prompt. On the same 30 regulatory bodies the news prompt
  also kept the one relevant item, but only as unsure, and kept a supplement-
  advertising case and CBAM guidance on the words *import* and *customs*. The
  regulatory prompt lists legal areas rather than business topics, shows no keyword
  brackets, and treats a digest as relevant if any one item is.
- About 1,300 input tokens per news body, 1,800 per regulatory body.

The shipped prompts, one pass over the same 90 labels after the changes above
(competitor narrowed to its parcel, express and e-commerce business, body false
matches added, regulatory wording made client-neutral): news kept 15 of 15
relevant and dropped 24 of 35 irrelevant, where the prompts measured above had
kept 16 of those 35; regulatory kept its 1 relevant and dropped 26 of 30. The
narrowing dropped both DHL appointments and the warehouse line both warehouse
stories. The legal-Q&A line did not bite - both returns Q&As were kept as parcel
liability - and three of the eleven news false keeps are BPEX pages the config now
excludes. The regulatory keeps are four unsure hedges: public procurement twice,
CBAM, and a consumer case whose body is only teaser text.

Regulatory, one pass of the regulatory prompt over all 52 regulatory bodies the
selector picked and all 301 it skipped in the window:

- Four were relevant, and the selector had picked all four: the Bundesnetzagentur's
  DSA findings against eBay (trader traceability), a Commission daily-news item on
  the GPSR sweep of online marketplaces, "EU VAT rules for e-commerce, five years
  on" (IOSS figures), and EUCDM 7.0.1, which adds the temporary duty on consignments
  up to €150.
- None of the 301 skipped bodies is relevant on reading. The gate kept 13 of them
  anyway (4 relevant, 9 unsure): Verbraucherzentrale's sidebar teasers - a
  product-recall blurb sits on 158 of its 203 case pages - and the word *Post* in
  mail-forwarding cases were enough. On noise the gate keeps about 4 %, so the
  selector in front of it roughly halves what would reach the full assessment.
- 30 of the 52 regulatory picks matched only ambient policy words (*regulation*,
  *import*, *supply chain*, *third country*); none of the four relevant items was
  among them. Postal-regulation vocabulary (*Porto*, *Briefentgelt*,
  *Universaldienst*, *Deutsche Post*) is not in the profile, and no postal decision
  appeared in the window, so recall there is untested.

So regulatory and full-text news bodies reach the gate through a recall-first
keyword filter in front of a precision-first model; fetched title-gate keeps take
the already-approved title route instead. The selector's regulatory picks are 92 %
noise, but the gate discards that cheaply; the gate's 4 % keep rate on noise is what
the selector spares the full assessment. The selector module was renamed from
`src/news_selector.py` to `src/selector.py` for serving both tiers. The check to
repeat now and then is the one above - gate the skipped regulatory bodies once and
read what it keeps - because that is where missing vocabulary would show.

### Parliamentary procedures

Added 2026-09-11; the measurements are in `docs/source_coverage.md`, "Parliament".
DIP procedures and European Parliament procedures are stored as regulatory items
whose body is composed from the record - type, state, subject descriptors,
abstract, dated steps - so they take the regulatory selector and gate unchanged,
with two differences:

- The EP source has `keyword_prefilter: false`. Every procedure is a candidate,
  because the set is small - a few hundred, a handful new a month - and titles such
  as "Clean corporate vehicles" match no keyword. The gate is that source's
  selector, which is also why collecting all of them costs nothing extra: the
  choice of which procedures matter is made per client, not in `input/`.
- The regulatory prompt was widened the same day to name parliamentary material and
  to count a question put to the government in the client's areas. An unanswered
  minor question on customs checks of Temu parcels announces and decides nothing,
  but it is the political signal. The 52 regulator bodies gated earlier keep their
  decisions under the old prompt version and were not re-gated.

Relevance is decided once per procedure. The gate identifies an item by source and
external id, so a new step - a new version - is not gated again, and that is
intended: a procedure's topic does not change when the Bundesrat votes on it. What
happened this week is the report's question. It reads the versions stored in its
window of procedures the gate kept, and names the step from the body.

First pass: DIP 52 selected, 6 relevant, 7 unsure, 39 irrelevant; watch-list 14,
7 / 5 / 2. Two DIP keeps are false, EU-US tariff regulations forwarded as EU
documents. The one recall miss found is the postal vocabulary gap above, now
measured: a written question on automated stations replacing postal branches,
tagged *Postfiliale*, selected by nothing.

Second pass, 2026-09-12, after EP collection widened from those 14 to every
legislative procedure: 224 gateable procedures, 8 relevant, 31 unsure, 185
irrelevant. The 14 expectations held with no miss, and the gate kept one procedure
the hand review had passed over, 2025/0348(CNS) on prosecutor access to VAT data -
a weak keep. So the gate is a fair stand-in for a hand review at this volume, which
is what lets collection stay client-neutral.

Only a relevant verdict goes on from here: for parliamentary procedures, unsure
stops like irrelevant. Decided 2026-09-11 on the first pass. The documents behind
all 7 DIP procedures the gate left unsure carried nothing for the client:
settlement imports, two supply-chain-law questions, investment screening, an
EU-India trade question, the deforestation bill (its six *Sendungen* are timber
consignments), and a customs-and-financial-crime question whose 31,000 characters
of question and answer never mention parcels, platforms or e-commerce. The 5
unsure watch-list procedures are the list's own fleet and cross-sector entries:
clean corporate vehicles, van CO2, heavy-vehicle weights, posted workers, the
Digital Omnibus. Every real signal came as relevant, and so did the noise that
costs something, the two tariff regulations. Unsure stays stored, to be skimmed
during the pilot like the drops. The prompt is unchanged: it is shared with the
regulators, and a model allowed to hedge keeps borderline items out of relevant.

### Documents behind parliamentary procedures

`python run.py fetch-dip-docs --client <client>` (`src/dip_documents.py`) runs in
`run_daily.bat` after the body gate. For each DIP procedure whose latest regulatory
body-gate decision for the client is relevant, it fetches every Drucksache the
newest version references - the government's answer, the bill, the committee
report - once per DIP document id, shared by every client. Relevance is decided
once per procedure, but the document list comes from the newest version, so an
answer that arrives later is fetched too. Plenary protocols and list entries are
skipped; what the steps reference is measured in docs/source_coverage.md,
"Parliament". The text is stored whole in `dip_document`; the storage and retry
contract is in docs/body_collection.md, "Parliamentary documents".

What the full assessment reads is cut from the stored text when it is read:

| Document | Read as | Measured 2026-09-11 |
|---|---|---|
| Written question, in a collective Drucksache | the one question and its answer, by `frage_nummer` | 750-4,500 of 171,000-627,000 characters |
| Bill, ordinance, committee report | the opening summary, from "A. Problem" to the draft, the recommendation, or the Bundesrat header repeated above the cover letter | 5,400-6,800 of 154,000-1,112,000 for three bills; 1,600-1,800 for two committee reports, recommendation included |
| Anything else | from the start, up to 30,000 characters, marked when cut | an answer to a minor question, 21,600, whole |

A procedure's documents, newest step first and within 40,000 characters, are its
excerpt; a document not fetched yet is named, so the reader knows it exists.
`fetch-dip-docs --show <procedure id>` prints the record and the excerpt as the
full assessment will read them. Nothing reads them yet.

A bill's summary does not always say what matters to the client. The customs bill's
summary is about the Generalzolldirektion and money laundering; the obligation for
postal and parcel operators to give customs investigators sender, weight, tracking
number, pickup-station number and time-and-place data for a shipment sits in one
amended paragraph, about 2,600 lines into 1.1 million characters. Passages around
profile keywords do not find it: the profile has no postal vocabulary, and in that
bill *customs* hits 2,146 times and the two body-only policy rules 671 and 618.

Nothing is built for that, on purpose. It is one observed case; the record itself
was not blind to it, since DIP's descriptors name *Brief-, Post- und
Fernmeldegeheimnis* and the summary names the extended investigative powers of the
Finanzkontrolle Schwarzarbeit, which audits the KEP sector; and a passage found
this way would be evidence for the assessor rather than anything a customer reads.
See todo.md before building it.

## Safety Gate

Decided 2026-09-12 from a hand review of the whole stored pilot window: every
alert notified by Germany with a Chinese country of origin over the 12 weekly
reports from 2026-06-26 to 2026-09-11, 50 in all. Labels are in
`clients/jt-express/labels/safety_gate_2026-09-12.json`.

| Label | Alerts | Per week |
|---|---:|---:|
| direct — J&T named or shown to have carried the item | **0** | 0 |
| customer exposure — `onlineTrader` names a key customer | 20 | 1.7 |
| market trend — Chinese-origin good acted on, no key customer named | 27 | 2.3 |
| irrelevant | 3 | 0.3 |

**`direct` is a structural zero, not an unlucky window.** No carrier is named in
any of the 639 stored alerts, J&T or any competitor. Safety Gate records the
product, the brand and the online trader; the carrier is not a field, because the
duty falls on sellers and platforms. This is the same structural zero as the DSA
database and it has the same consequence: Safety Gate can never produce an
`own_brand` item for the alert push. A marketplace match does not prove J&T
carried the item, and nothing in the record ever will.

**The useful scope is the `onlineTrader` field, not the product.** Temu, Shein,
AliExpress and TikTok Shop are `role: customer` in `profile.json` — J&T delivers
for them — so an alert naming one is the customer's `key_customer` monitoring
category (重点客户动态监测), which the taxonomy already pushes by newsletter. The
split is AliExpress 11, Temu 5, Shein 4; TikTok Shop appears in none of the German
50 and in 7 alerts notified elsewhere. 15 of the 20 list *Removal of this product
listing by the online marketplace* among their measures: the key customer itself
acted. The remaining 27 alerts are a sector fact about the goods flow J&T carries,
which is what the `industry_policy` category is for. Product, brand, barcode and
SKU are evidence inside the item, not a scope.

**Treatment is clustered weekly, and immediate alerting is ruled out on the
data.** The lag between a measure taking force and Safety Gate publishing it is a
median of 30 days measured from the earliest measure and 28 from the latest
(min 4, max 191; at most 2 of 47 within 7 days). 29 of the 50 carry more than one
measure, so the lag is a range per alert rather than a single number. A weekly
retrospective bulletin cannot carry an immediate alert whatever the content. The
pilot view is also 50 of 50 *Serious risk*, so severity ranks nothing, and the
product classes repeat: 10 of 50 are balloons, 9 adaptors or extension leads, 5
bicycle helmets, 3 sand-filled toys. Per-item alerting would send four near
duplicates a week. The report therefore carries one clustered block, key-customer
items named individually and the rest grouped by product class and risk.

**Germany-only remains the rule, with a measured cost.** Widening to every
notifying country adds 53 key-customer alerts (France 29, Luxembourg 10, Ireland
5), and **none of the 53 repeat a product/brand pair already in the German set** —
so this is not deduplication, it is a different and larger set. Whether a Temu
listing pulled in France is a J&T Germany item is a client scope question, not a
collection one; see todo.md §2.

**Wiring, built 2026-09-12.** Safety Gate rows reach neither the selector nor the
body gate, and that is correct: the deterministic selector reads keywords over
titles and bodies, while these are structured records whose relevance is decided
by two fields. No relevance LLM call is justified when the geography rule already
yields 47 of 50 usable items. They enter the funnel at the full assessment, on the
`select_client_alerts` view, bypassing both gates.

```text
collect-safety-gate -> raw_item -> select_client_alerts -> full assessment
                                   (geography + onlineTrader,     |
                                    compose_body, no LLM)     assessment row
```

`select_client_alerts` returns each alert with `key_customers`, parsed `measures`
and a composed `body_text`. `python run.py safety-gate-view [--key-customers]
[--since] [--full]` prints that view and is read-only: no assessment rows, no
model or network calls. It exists so the source is inspectable before the
assessor is built, and remains the way to check the client rules after it is.

`compose_body` follows `src/dip.py` and `src/ep_procedures.py` — a deterministic
template over the record, no summarising model, because an LLM would add cost and
a fabrication risk to fields that are already prose. It differs from those two in
where the body lives: they store `body_text` in the payload because the body gate
reads it straight out of SQL, whereas Safety Gate skips that gate, so the body is
composed on read. That keeps one copy of the text, lets a template fix reach
alerts already stored, and needs no backfill.

Two traps in the `measures` field, both found by composing a body and reading it:

- It is one run-together string — label, value, next label, no separators — and
  **29 of the 50 pilot alerts carry more than one measure**, so reading only the
  first undercounts marketplace removals. The parser locates labels rather than
  splitting on them, because the string does not reliably start with one.
- There are two live spellings of the operator label (505 and 358 of 639 alerts),
  and the date is the literal string `Unknown` in 267 of 962 measures. An
  undatable measure is stored as no date rather than as that word, which is the
  same rule the rest of the project applies to dates.

All 639 stored alerts parse into 962 measures, every one with a category; 695
carry a real date and 267 none.

## Historical funnel backfill

Historical processing is an operator campaign, not a daily-pipeline mode. Use
`tools\\backfill_funnel.bat`; it holds `data\\run.lock`, and no command from the
tool is called by `run_daily.bat`.

The campaign freezes the selected news bodies, selected regulatory bodies and
title-only candidates without bodies under one client profile and prompt set.
Title decisions go to an isolated campaign directory under
`data/backfill/funnel/`, not to the live `data/title_gate/` handoff. They are
bounded and resumable. Keeps cannot be promoted until every frozen title has a
decision. Promotion is also bounded, so a fetch limit of 20 introduces at most 20
new historical keeps to the durable body queue rather than queuing the whole
archive and merely limiting HTTP requests.

```text
tools\\backfill_funnel.bat create initial-2026-09 --client jt-express
tools\\backfill_funnel.bat status initial-2026-09
tools\\backfill_funnel.bat title-gate initial-2026-09 --limit 100
tools\\backfill_funnel.bat body-gate-existing initial-2026-09 --kind news --limit 100
tools\\backfill_funnel.bat body-gate-existing initial-2026-09 --kind regulatory --limit 100
tools\\backfill_funnel.bat fetch-title-keeps initial-2026-09 --limit 20
tools\\backfill_funnel.bat shadow-body-gate-title-keeps initial-2026-09 --limit 20
```

Repeat the bounded commands until `status` reports no remaining work. Creating a
campaign and checking status make no model or network calls. The other commands
are deliberately explicit; a changed profile, prompt, model, reasoning setting or
source configuration invalidates the frozen campaign instead of mixing versions.

Body-gate verdicts do not mark the shared `raw_item`. They are customer-specific
rows in `assessment`, keyed by raw item, client, prompt version and profile
version. A later full assessor writes another customer-specific assessment row
under its own prompt version, and the report reads those full-assessment rows.
The body gate's `relevant` and `unsure` rows, plus successfully fetched title-gate
keeps, define what may reach that expensive stage; irrelevant rows remain queryable
for audit.

`shadow-body-gate-title-keeps` is an evaluation command only. It applies the
current news body-gate prompt to successfully fetched title keeps and appends the
results to the campaign's `title-body-gate-shadow.jsonl`. It writes no
`assessment` rows and does not change the direct-to-assessor route. Re-running it
continues with keeps that do not yet have a shadow verdict.

## Weekly assessment and report

`src/report_agent/` is the stage after the gates. Four commands, and only the
second calls a model:

```text
run.py export-window --client --since --until   frozen bundle (read-only)
run.py assess        --bundle                   decisions + issue register
run.py report        --bundle                   zh report, ledger, coverage
run.py verify-report --bundle                   verification.json, exit 1 on error
```

The shape came out of a hand-made cycle for 2026-09-05..11 whose scaffolding is
in `data/reports/experiment-2026-09-05_11/`; `report_plan.md` records what that
demonstrated. The short version is that the artefact it left behind was an
evidence-freezing harness plus a rendering harness with an analyst-shaped hole
between them, and the hole is the only part that needs a model.

### Relevance is not treatment

The gates answer *is this a signal for this client*. The report needs a second
and different answer — what happens to it this week:

| Treatment | Meaning |
|---|---|
| `report` | carries its own finding |
| `merge` | folded into another item's story |
| `background_only` | context inside a finding, not reported alone |
| `carry_forward` | an open issue with no qualifying development this week |
| `insufficient_evidence` | reportable in principle; the stored material cannot carry it |
| `omit_for_priority` | real and client-adjacent, not worth the week |

`merge` and `carry_forward` are structurally impossible for a per-item scorer,
which is the same conclusion §1.1 of `todo.md` reached from the cadence side.
The bulk treatments beside these — `retain_gate_stop`, `archive_no_week_update`,
`background_not_reported` — are written by rules. In the reference week 908 of
1,004 rows were bulk, and paying a model to say `archive_no_week_update` two
hundred times buys nothing.

### The six steps

Carry-forward and deep read and challenge are agentic: they decide what to read
next from what they just read. Triage, cluster and write are fixed calls, the
same pattern as the gates. State between steps is artefacts on disk, never a
preserved conversation — a transcript is not traceable to a prompt version, is
not re-runnable, cannot be verified as keyed rows, and lets one story's framing
bleed into the next.

The tool surface is five read-only calls over the frozen bundle: `get_item`,
`get_body`, `search_titles`, `search_bodies`, `open_issues`. No network at any
point in the stage.

**Search reaches the items a gate stopped.** This is the one counter-intuitive
part and it is deliberate. A continuing story often fails a per-item relevance
test: a port-strike article that never mentions parcels is correctly stopped as
non-parcel-specific, and is still the second instalment of an issue already
open. The carry-forward step is bounded by the open issues rather than sweeping
the rejects, so the gate's drops are searched *for a named thing*, not reopened
wholesale.

### What makes the output checkable

- **Complete accounting.** Every identity in the export carries exactly one
  ledger row. The renderer fails on a gap rather than rendering a partial
  ledger; a report claiming eight findings without saying what happened to the
  other 996 cannot be audited.
- **Links resolve by id.** The writing step emits `[文字](item:24617)` and the
  renderer substitutes the URL the export froze for that id. A URL the export
  does not contain fails the build. Fabricating a source is therefore structurally
  impossible rather than something a reviewer has to catch.
- **Depth is measured.** `review_depth` comes from the assessor's logged tool
  calls — `get_body` was called, so `full_stored_body`; nothing was, so
  `title_only`. The hand-made cycle inferred depth from hardcoded id sets, which
  made the ledger's most important column a claim.
- **Constants come from the manifest.** The experiment's verifier asserted that
  week's counts as literals, so its checks were true exactly once.

### The issue register

`issue-register.json` is the unit of state between weeks: dated status, the
evidence behind it, and the evidence that would trigger the next update. The next
cycle starts from that rather than from last week's prose. It is a file per cycle
for now, found by scanning earlier bundles for the latest one that closed before
this window opened; moving it into SQLite is a reader change in
`src/report_agent/bundle.py` and nothing else.

### Model

`gpt-5.6-sol`, pinned in `config.json` with reasoning effort per step. Not the
gates' model: the gates are a cheap screen in front of this, while this stage
reads bodies, challenges its own first reading and writes the customer-facing
Chinese. **It has no dated snapshot published**, which is the one place this
project's pinning convention cannot be honoured, so every bundle's
`assessment-manifest.json` records the model and the per-step prompt hashes to
make an upstream change visible after the fact.

## Remaining design decisions

- Full assessment model and prompt, and how it reads the body gate's decisions.
- How the full assessment finds the passage of a long parliamentary document that
  matters to the client, where the document's summary does not say it.
- Structured output required by weekly reports and immediate alerts.
- When a profile-version change triggers reassessment of historical items.
- Alert cadence the customer is promised. `01519` is answered and needs nothing
  built: it is J&T Global Express's Hong Kong stock code, and no German news
  source uses it (`clients/jt-express/alert_taxonomy.json`).

**Decided 2026-09-12: the full assessment runs weekly over a window of
`raw_item.fetched_at`, not daily and never over `published_at`.** The assessor is
the only stage that touches no network, so it alone can be re-run at will — the
`assessment` key makes re-assessment additive, and `fetched_at` is on 100 % of
stored rows where `published_at` misses 2.6 %. `assessment.created_at` is rejected
as the window because a re-run would stamp it with the day it ran. The reasoning,
the volumes and the accepted risk are in todo.md §1.1.
