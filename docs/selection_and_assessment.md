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

## Assessment funnel

```text
title-only news           full-text news            regulators
      |                         |                        |
deterministic selector    deterministic selector    deterministic selector
      |                         |                        |
title gate (built)              |                        |
      |                         |                        |
fetch body (not built)          |                        |
      +------------+------------+                        |
                   |                                     |
       body gate, news prompt (built)     body gate, regulatory prompt (built)
                   +------------------+------------------+
                                      |
                          full structured assessment
                                      |
                             Chinese report/alert
```

The title gate sees only title-level evidence and decides whether a body is
fetched. Every body - fetched on a match or stored by a full-text source - then
passes the body gate, which decides whether it is worth the full assessment.
Regulatory records skip the title gate because their relevance is topical, and use
their own body-gate prompt.

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
- **No memory beyond the run.** A profile change applies to new items; `gate --all`
  re-gates the corpus deliberately, `gate --run N` redoes one day.

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

- **Input.** The selector's news and regulatory candidates that carry a stored body
  and have no body-gate decision yet, newest first, at most `limit` per tier and run
  (config.json). It reads every such body rather than only today's, so a body that
  arrives on a retry is gated when it arrives. An item is its source and external
  id: a restamp's version 2 is not re-offered.
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

So regulatory bodies reach the gate through the selector, as news bodies do: a
recall-first keyword filter in front of a precision-first model, which make
different mistakes. The selector's regulatory picks are 92 % noise, but the gate
discards that cheaply; the gate's 4 % keep rate on noise is what the selector
spares the full assessment. The selector module was renamed from
`src/news_selector.py` to `src/selector.py` for serving both tiers. The check to
repeat now and then is the one above - gate the skipped regulatory bodies once and
read what it keeps - because that is where missing vocabulary would show.

## Remaining design decisions

- How fetch-on-match queues explicit title-only items regardless of `content_mode`.
- Full assessment model and prompt, and how it reads the body gate's decisions.
- Structured output required by weekly reports and immediate alerts.
- When a profile-version change triggers reassessment of historical items.
- Alert cadence and the meaning of the customer's identifier `01519`.
