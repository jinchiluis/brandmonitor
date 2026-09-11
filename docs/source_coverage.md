# Source coverage report — J&T Express, first source list

Probed 2026-09-08 against the client's first list (24 named outlets across three
tiers). Every figure below is measured, not estimated: each site was crawled with
`python run.py probe`, which runs all three discovery methods and reports what came
back. Raw results are in the probe output; the resulting configuration is
[input/germany_medias.json](../input/germany_medias.json).

## Headline

| | |
|---|---|
| Sources on the list | 24 |
| Sweepable by sitemap | 19 |
| Sweepable by feeds only | 3 |
| Frontpage fallback only | 2 |
| Nothing works | 0 |
| Publishers known to contain paid sections | at least 10; access is usually mixed |

The list is in far better shape than the earlier tagesschau result suggested. Every
named source can be collected by some method.

## Per-source verdict

Counts are articles discovered in a 14-day window. `feeds` counts entries in the
feed, which is not window-limited.

> **Correction, 2026-09-09.** The sitemap counts in the three tables below were
> produced before the duplicate-sitemap fix (see PROVENANCE). For the five sources
> whose robots.txt declares no sitemap — **BVL, FAZ, LOGISTIK HEUTE, WELT,
> Verbraucherzentrale** — the crawler discovered every URL twice, so their sitemap
> figures here are roughly **2× too high**. FAZ's "885/14d" is nearer 440;
> LOGISTIK HEUTE's "238" nearer 119. WELT and BVL hit the 1,500 cap either way, so
> their true volume is unknown but at least ~750.
>
> The **signal yield table further down is unaffected** — that measurement
> deduplicated by URL before counting, so its article totals and brand-hit counts
> stand as published. Only the crawlability tables and the volumes recorded in each
> source entry's `notes` need re-measuring.

### 德国主流媒体 (mainstream)

| Outlet | sitemap | feeds | Verdict |
|---|---:|---:|---|
| Handelsblatt | 1500+ | — | sitemap (paywalled) |
| WirtschaftsWoche | 1160 | — | sitemap (paywalled) |
| DER SPIEGEL | 1500+ | 72 | sitemap + feeds (paywalled) |
| Süddeutsche Zeitung | 0 | 0 | **frontpage only** (paywalled) |
| FAZ | 885 | 164 | sitemap + feeds (paywalled) |
| DIE ZEIT | 1500+ | 30 | sitemap + feeds (paywalled) |
| WELT | 1500+ | — | sitemap (paywalled) |
| Tagesschau/ARD | 0 | 143 | **feeds only** — free |

Süddeutsche is the weak one: no sitemap, no feeds, frontpage only. Its frontpage
does yield usable sections, so it is configured with an explicit navigation seed.
Worth revisiting — a paid subscription may expose a content feed that anonymous
access does not.

### 德国物流及电商行业媒体 (trade press)

| Outlet | sitemap | feeds | Verdict |
|---|---:|---:|---|
| DVZ | 32 / latest 2d | — | undeclared Google News sitemap, title-only/mixed access |
| VerkehrsRundschau | 232 | — | sitemap |
| LOGISTIK HEUTE | 238 | 20 | sitemap + feeds |
| Onlinehändler-News | 81 | — | sitemap |
| etailment | 698 | — | sitemap |
| t3n | 0 | 30 | feeds only |
| e-commerce Magazin | 22 | 120 | sitemap + feeds |

DVZ's gap was confirmed and closed 2026-09-11. A configured crawl of the sitemap
declared in `robots.txt` returned only 10 URLs in 14 days, while the separately
published `news-sitemap.xml` contained 32 titled articles with real publication
dates from September 9–10. `extra_sitemap_urls` now adds that verified root without
probing guessed paths on every source. Because the recovered stream is mixed-access,
DVZ is `title_only`: selected URLs will be fetched on match, never as a bulk paid
archive crawl. Like every two-day Google News sitemap, it requires daily collection.

### 德国行业协会 (associations)

| Body | sitemap | feeds | Verdict |
|---|---:|---:|---|
| DSLV | 5 | — | sitemap |
| BGL | 2 | 30 | feeds (sitemap is near-empty WordPress noise) |
| BPEX | 0 | 0 | frontpage — **see correction below** |
| BVL | 1500+ | 47 | sitemap + feeds |
| HDE | 0 | 8 | feeds only |
| bevh | 117 | — | sitemap |
| BVDW | 109 | — | sitemap |
| Händlerbund | 124 | — | sitemap |
| Wettbewerbszentrale | 33 | 30 | sitemap + feeds |

## Two corrections to the source list

**BPEX was pointing at the wrong company.** `bpex.de` is an 11-page consulting and
software firm, not a trade body. The association the client means is the former
BIEK (Bundesverband Paket und Expresslogistik), which renamed to BPEX and sits at
**`bpex-ev.de`** — `biek.de` redirects there. For a parcel-carrier client this is
one of the most relevant sources on the entire list, so monitoring `bpex.de` would
have quietly substituted a random consultancy for the sector's main association.
The config now uses `bpex-ev.de`, seeded on `presse`, `aktuelles`,
`themen-und-positionen` and `kep-branche`.

Frontpage discovery keeps every link a seed page carries, including the site
navigation, so on 2026-09-11 38 of the 57 stored BPEX rows were about, legal,
listing and pagination pages, and the selector passed 37 of the 57 on to the LLM
stages. The entry now excludes those sections and `/aktuelles?`. What remains is
the press releases under `/presse/meldung/`, the rare `/aktuelles/meldung/` item,
the position pages under `/themen-und-positionen/` and the KEP figures page. The
nine excluded `/aktuelles?file=` PDFs duplicated the nine HTML releases one to
one; they were the only BPEX rows carrying a date.

**Onlinehändler-News and Händlerbund are one organisation.** `onlinehaendler-news.de`
redirects to `ohn.haendlerbund.de`. Both are kept, since the publication and the
association publish different things, but "we monitor 24 sources" is really 23
organisations.

## Applied source-policy corrections

These decisions are production configuration, not pending work:

- **Verbraucherzentrale** was narrowed to `verbandsklagen`, `urteile`, and
  `wissen/vertraege-reklamation/abzocke`. This reduced a measured 30-day result from
  766 broad consumer pages to 203 event-like records. The 563 excluded rows were
  purged after a backup; collection and body backfill enforce the same prefixes.
- **BVL** was narrowed and ultimately changed to `title_only`. Its regenerated
  sitemap gives thousands of archive, furniture, and malformed URLs one shared
  `lastmod`, causing a permanent refetch treadmill. In 620 retained URLs it produced
  no brand hit and only three selector-worthy slugs. Index/malformed rows were
  purged; the useful de-minimis post remains discoverable by its slug.
- **etailment** is restricted to `/magazin`. A 2026 migration restamped most of its
  25-year archive, and 519 of the first 2,000 URLs were index pages. News sitemaps
  are now traversed first because they carry real titles and publication dates for
  the rolling recent window.
- **LOGISTIK HEUTE `/fachmagazin`** was excluded and purged on 2026-09-10. The
  section was subscriber-only and not valuable enough to retain as title evidence.
  The same durable policy now excludes 68 `/termine/` event listings and 21 company
  `Newsübersicht` indexes. A transactional repair removed 177 historical raw
  versions, normalized 1,021 retained page-date versions while preserving their raw
  values, and removed the terminal tag cloud from five retained gallery bodies.
  Discovery, body backfill, and candidate selection enforce the same rules.

- **The eight mainstream outlets** were given `excluded_dirs` on 2026-09-10, the
  first restrictions any of them had carried. Measured effect below. Rows already
  collected were **not** purged: exclusions are enforced at collection, body
  backfill and candidate selection alike, so the stored rows are inert, and they
  are the evidence for this table.
- **Bundesnetzagentur** was topic-filtered on 2026-09-10, closing the "needs topic
  filtering" note its own source entry had carried since the regulatory tier was
  proposed. The excluded prefixes are `DE/Allgemeines/Presse/Amtsblatt`,
  `DE/Fachthemen/ElektrizitaetundGas` and `DE/Fachthemen/Telekommunikation` — 74 of
  134 rows and 0.95 of 1.15 MB. Nothing postal was lost:
  `/SharedDocs/Pressemitteilungen` is untouched and `DE/Fachthemen/Post` is
  deliberately *not* excluded so postal topics survive if they ever appear. Note
  that this source is **feeds-only**, so `allowed_dirs` would have been ignored
  entirely (see CLAUDE.md's table) — only `excluded_dirs` works here.

### Measured effect of the 2026-09-10 restrictions

| Source | Rows | Excluded | | Body bytes removed |
|---|---:|---:|---:|---:|
| faz.net | 2,061 | 1,104 | 54% | 0.26 MB |
| spiegel.de | 1,998 | 800 | 40% | 0.20 MB |
| welt.de | 4,433 | 741 | 17% | 0.22 MB |
| handelsblatt.com | 917 | 270 | 29% | 0.07 MB |
| zeit.de | 4,459 | 247 | 6% | 0.04 MB |
| bundesnetzagentur.de | 134 | 74 | 55% | **0.97 MB** |
| wiwo.de | 420 | 48 | 11% | 0.01 MB |
| tagesschau.de | 144 | 45 | 31% | 0.01 MB |
| sueddeutsche.de | 361 | 39 | 11% | 0.00 MB |
| logistik-heute.de | 2,059 | 12 | 1% | **1.19 MB** |
| **Total** | **20,759** | **3,380** | **16%** | **3.04 MB** |

The two columns measure different wins and should not be read together. The
mainstream cuts are a **selection-cost** win — those sources are `title_only`, so
their rows are ~250 B stubs and removing 3,000 of them frees almost no disk but
takes 3,000 titles out of every selection prompt. The **disk** win is 86 rows:
logistik-heute's hubs and Bundesnetzagentur's gazette pages carry 2.16 MB of the
3.04 MB total.

Verified by running the production `url_is_excluded` over the stored corpus and
dumping every excluded row above 5 KB on a `full_text` source. All of it is hub
pages, `Amtsblatt 1..17/2026` gazettes, energy auctions ("Wind an Land:
Gebotstermin 1. Mai 2026"), mobile-telecom statistics, and a 93 KB *Verzeichnis der
zugeteilten deutschen Amateurfunkrufzeichen* — the register of German amateur-radio
callsigns. No legitimate content was caught.

**`welt.de/regionales` was considered and deliberately kept.** At 2,194 rows it is
over 10% of the entire corpus and is state-level local news, which is the profile of
pure noise. It stays because a parcel client runs regional depots, and a depot
opening or closure surfaces in state news before it reaches the trade press. Revisit
once a real cycle shows whether it ever produces a selected item.

**FAZ needs depth-2 prefixes.** 2,006 of its 2,061 rows sit under `/aktuell`, so a
one-segment rule separates nothing. `url_matches_dirs` matches on `path.startswith`,
so multi-segment values work: `aktuell/feuilleton` (607 rows), `aktuell/sport` (194)
and `aktuell/rhein-main` (266) are 52% of the source between them.

### Rolling hub pages need a title rule, not a directory rule

LOGISTIK HEUTE publishes per-company hub pages *under `/news`, beside real
articles* — "Bosch: Aktuelle Meldungen zu Produktion, KI, Robotik und Logistik" is
186 KB of concatenated teasers carrying 65 date-stamps, against a 2.8 KB article
median. No path prefix separates them, so `excluded_dirs` cannot reach them. The
existing `excluded_title_substrings` mechanism can, and was extended on 2026-09-10
to `["Newsübersicht", "im Überblick", "Aktuelle News", "Aktuelle Meldungen",
"News zu", "Alle News"]` — **12 rows, 1.19 MB, 26% of this source's body text, with
zero false positives across all 2,059 titles.**

The URL cannot be trusted here even in principle. One hub sits at
`/news/e-commerce-zalando-uebertrifft-dank-endspurt-gewinnprognose-fuer-2024-194044.html`
under the title "Zalando-Logistik: Alle News sowie aktuelle Entwicklungen…" — the
publisher repurposed a 2024 earnings article's URL into a rolling index. Only the
title and the body are honest.

**One hub still escapes**, and it defines the limit of a title rule: "Arvato
Logistik: Entwicklungen, Projekte und Kooperationen des Supply-Chain-Dienstleisters"
(134 KB, 50 date-stamps) reads exactly like an article. The generic alternative is
**date-stamp density in the body** — an article carries one date, an aggregation page
repeats one per teaser. Measured across the sources that fetch bodies:

| Source | median | p95 | max |
|---|---:|---:|---:|
| logistik-heute.de | 0 | 0 | 75 (hubs: 50–75) |
| verkehrsrundschau.de | 0 | 1 | 11 |
| etailment.de | 0 | 0 | 1 |
| verbraucherzentrale.de | 0 | 2 | 4 |
| bvdw.org | 0 | 3 | 10 |

A threshold of ≥15 separates cleanly with margin on both sides and would catch
Arvato — but it misses two hubs the title rule does catch (Safelog at 9 stamps,
Hyperloop at 6), so neither rule subsumes the other; the union catches all 13.
**Not built.** It would be a post-fetch gate in `src/bodies.py`, structurally
different from `is_furniture`, which runs on URLs at discovery. Config already
recovers 1.19 of the 1.31 MB and the residue is one row. Build it generically when
a second source shows the pattern — verkehrsrundschau already has a row at 11.

### Dating coverage — measured 2026-09-10

Of the 17,379 rows in the working corpus, **470 (2.7%) carry no publication date at
all**, and they are concentrated in three sources:

| Source | Undated | of | |
|---|---:|---:|---:|
| sueddeutsche.de | 315 | 322 | 98% |
| bpex-ev.de | 95 | 113 | 84% |
| bundesnetzagentur.de | 60 | 60 | 100% |

Every other source is fully dated. The pattern is discovery method, not publisher:
Süddeutsche and BPEX are the two **frontpage-only** sources, and frontpage links
carry no date — which is already recorded in Süddeutsche's entry as "80 links,
undated". This is a structural property of frontpage discovery, not a bug to fix.

The restamp audit CLAUDE.md prescribes — distinct publication *days* against row
count, and the lag from the busiest day to the fetch day — was run across all
sources. **The large `title_only` sources are clean**: welt.de spreads 3,692 rows
over 8 days (top day 18%, lag 1), zeit.de 4,212 over 11 (13%, lag 1), faz.net 957
over 32 (12%, lag 2). Their sitemap dates are real publication dates, not `lastmod`.

**BVL remains the documented exception**, now visible in stored data: 629 rows across
3 distinct days with 100% on one. That is the shared-`lastmod` restamp already
recorded above, and because BVL is `title_only` no page date will ever overwrite it.
Those 629 rows are not undated — they are *confidently wrongly dated*, which is the
worse failure. Treat BVL dates as unusable for any recency gate or customer-facing
date.

Provenance gap, closed 2026-09-11: `published_at_source` was only written during
body fetch, and as `discovery` it did not say whether a feed `<pubDate>` or a
sitemap `<lastmod>` supplied the date. Collection now labels every row at discovery
(`feed`, `news_sitemap`, `lastmod`, `frontpage`, or null) and a body fetch
overwrites it with `page`. Rows stored before that date keep `discovery` and are
resolved from `discovered_via`; their sitemap flavour is unrecoverable and stays
`sitemap`.

### What the non-page-dated bodies actually were — measured 2026-09-11

Of 2,672 stored bodies, 1,966 carried a page date. The rest split as follows,
which is a different picture from "444 rows the extractor missed":

| What the row is | Bodies | Where |
|---|---:|---|
| Feed-dated articles, date trustworthy | ~120 | EC press corner, vzbv, Bundeskartellamt, BEUC, EDPB, HDE |
| Verbraucherzentrale case records | 202 | 183 Verbandsklagen, 18 Urteile |
| Hub, event, member, download, evergreen pages | ~215 | bevh events 52 and Rechtshilfen 46, WBZ category archives 22, DVZ event and media-kit pages 22, BVDW person and download pages 19, VR section indexes 8, etailment section indexes 7, HB Termine 6 |
| Real articles on a template the extractor missed | ~10 | bevh /detail 7, DSLV Meldung 3 |
| LOGISTIK HEUTE editorial newsletters, no structured date | 4 | |
| Undated | 113 | BNetzA 66, 36 of them since excluded by config; BPEX 47, mostly pagination and about-pages |

The extractor share was about ten rows, fixed by reading a lone `<time datetime>`.
The hub share was handled by narrowing eight source entries on their stored path
histograms (bevh, DSLV, BVDW, Wettbewerbszentrale, DVZ, Händlerbund, ohn, LOGISTIK
HEUTE - the notes on each entry record what was dropped) and, for what config
cannot reach, by the selector gate described in `docs/selection_and_assessment.md`.

Two things the narrowing check caught that a plain "page-dated means article"
assumption would have missed: BVDW's WordPress stamps a JSON-LD `datePublished` on
every page, events and about-pages included, so 89 of its 95 page-dated rows were
not articles; and Händlerbund keeps real press releases under
`/de/news/presse/pressemitteilungen`, so only the mention and study indexes beside
them are excluded.

Verbraucherzentrale case records have no publication date at all. Their
`<time>` elements are the filing, service and status dates of the case, and the
`/urteile` pages carry only a visible "Stand:" line. Their `lastmod` stays stored
as a change signal, labelled as such, and is never printed as a publication date;
assessment reads the dates in the body.

When purging collected rows, remember that collection watermarks are independent.
Deleting `raw_item` while leaving an eligible URL and its watermark unchanged can
create a permanent gap; make the future collection rule durable before deleting.

## Regulatory tier — proposal

The contract commits to customs, consumer protection, product safety, competition,
data protection and EU platform regulation, but names no sources, and no collection
code exists for any of it. Measured findings on candidates:

**EU Safety Gate is the strongest source available and should be first.** It has an
official XML API listed on the EU Open Data Portal:

```text
https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en
```

1,114 weekly reports back to 2022, so historical backfill works. Each alert carries
`product`, `brand`, `barcode`, `riskType`, `danger`, `measures`, `countryOfOrigin`,
`notifyingCountry`, a recall URL, and — critically — `onlineTrader`. Measured over
the most recent 12 weekly reports:

| | |
|---|---:|
| Alerts total | 680 |
| Chinese origin | 471 (69%) |
| Notified by Germany | 131 |
| **Germany + Chinese origin** | **53** (~4.4/week) |
| `onlineTrader` names Temu | 26 |
| `onlineTrader` names Shein | 21 |
| AliExpress / Amazon | 43 / 94 |

Set against the news side, where `"J&T Express"` returned **zero** German media
mentions in 30 days, this one free official source yields more actionable items per
week than the entire 24-site news sweep will produce in direct brand mentions.

**Implemented and live-validated 2026-09-11.** `python run.py collect-safety-gate`
fetches the official report index, resumes from a report watermark, and stores every
alert under `source_kind = "safety_gate"` with its native fields intact. Each report
commits independently, so a stopped or partially failed historical run resumes
without losing completed work. Explicit `--weeks`, `--end`, and `--max-reports`
options support fixed-window validation and bounded backfills.

The current 12-report window contained 639 alerts. The J&T pilot view selected 50
that were both Germany-notified and Chinese-origin; 20 of those matched a customer
marketplace in `onlineTrader` (AliExpress 11, Temu 5, Shein 4). Collection itself
stores all alerts and remains customer-independent; the geography and marketplace
rules are applied later by `select_client_alerts` using the versioned client profile.
Re-fetching an identical alert writes nothing, while a changed official payload
appends a version.

### The contract's six domains

`德国及欧盟监管信息监测` names no sources. Its operative sentence is a list of
**subject domains**: product safety, consumer protection, competition, data
protection, cross-border parcels, and platform regulation — for German authorities,
EU institutions, and `公开信息` (public information) only. The table below maps each
domain to sources verified working on 2026-09-09.

| Domain | Source | Endpoint | Verified |
|---|---|---|---|
| 产品安全 product safety | **EU Safety Gate** | `/safety-gate-alerts/api/download/weeklyReport/list/xml/en` | 1,114 reports |
| 平台监管 platform reg | **EU Commission presscorner** | `/commission/presscorner/api/rss?search?language=en&policyarea=23` | 100 entries |
| 竞争 competition | **Bundeskartellamt** | `/DE/Service/RSS/_documents/rssnewsfeed.xml` | 30 entries |
| 数据保护 data protection | **EDPB** | `edpb.europa.eu/rss.xml` | 10 entries |
| 跨境包裹 customs | **EU Taxation & Customs** | `taxation-customs.ec.europa.eu/node/2/rss_en` | 30 entries |
| 跨境包裹 postal reg | **Bundesnetzagentur** | `/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/RSSNewsfeed_{Pressemitteilungen,Aktuelles_neu,Amtsblatt}.xml` | 10 / 50 / 17 → 67 distinct |
| 消费者保护 consumer (EU) | **BEUC** | `beuc.eu/rss.xml` | 10 entries |
| 消费者保护 consumer (DE) | **vzbv** | `vzbv.de/rss.xml` | 10 entries |
| 消费者保护 consumer (DE) | Verbraucherzentrale | sitemap | 5,057 URLs |

Five of the six domains are collectable today with no credentials. BNetzA's feeds
are mostly energy (Kohleausstieg, Gasversorgung), so its postal content needs topic
filtering — the volume is not the signal.

The three BNetzA figures are per feed and overlap: ten press releases appear in more
than one, so the source yields **67 distinct items, not 77**. That pattern is general
— any source configured with several discovery methods finds some articles through
each of them. Counts taken per method are therefore ceilings, and only a count taken
after de-duplication is a source's real volume.

The Presscorner endpoint was narrowed 2026-09-11 with the Commission's native
`policyarea=23` filter (Digital economy and society). A live crawler read returned
100 dated entries and included current DSA enforcement against TikTok and AliExpress.
The policy area also contains AI, connectivity and DMA material, so downstream
selection still decides client relevance; brand-only filtering at collection would
lose sector-wide platform rules.

### How each is collected

Not everything fits the news crawler. Three mechanisms, in order of preference:

**1. The vendored crawler, unchanged.** Anything that exposes a sitemap or a
discoverable feed: Bundeskartellamt (advertises its feed), EDPB, BEUC and vzbv (at
`/rss.xml`, a conventional path), Verbraucherzentrale (sitemap). Configure as an
ordinary source entry.

**2. The vendored crawler with `feed_urls`.** EU Taxation & Customs, the Commission
press corner and Bundesnetzagentur publish feeds that are neither advertised nor
conventional. Verified working after the change: 30, 50 and 27 items respectively.

**3. Its own collector.** EU Safety Gate is not articles — it is structured records
with `product`, `brand`, `barcode`, `riskType`, `countryOfOrigin`, `onlineTrader`.
Forcing it through `ArticleRecord` would discard exactly the fields that make it
valuable. The same applies to the DSA API. This is what `vendor/govcrawler/` is for
in the planned tree, and it matches the design rules: regulatory and reputation
processing stay independent, and `mvp_plan.md` §4 already says the raw store holds
"versioned raw source items" whose payloads may differ — *do not force them into one
business schema*. So: separate collectors, one shared raw-item envelope
(source id, url, fetched_at, payload), payload shape per source type.

### The DSA Transparency Database — access granted, measured 2026-09-09

Token held (`DSA_KEY` in `.env`), collector implemented as `src/dsa.py`, run with
`python run.py collect-dsa`. Everything below the endpoint table was measured
against the live API on 2026-09-09, and **most of the previously recorded
constraints were wrong** — they are corrected in place and flagged.
Temu, Shein and AliExpress are designated VLOPs, so every content-moderation
decision they take is filed here as a statement of reasons.

The 30-day production backfill completed 2026-09-11: 120 platform-days covering
2026-08-12 through 2026-09-10, with 30 days each for Temu, Shein, AliExpress and
TikTok and no failed days. The primary watermark is `2026-09-10`. Amazon and Zalando
remain disabled; every stored payload carries the required CC BY 4.0 attribution.

**The API**, once you hold a token — base
`https://transparency.dsa.ec.europa.eu/api/v1/research`, header
`Authorization: Bearer <token>`:

| Endpoint | Verified behaviour |
|---|---|
| `POST /sql` | **The one to use.** Elasticsearch SQL; `GROUP BY` returns a whole day's composition in ~600 bytes. Body is `{"query": "SELECT …"}`. |
| `POST /count` | Exact count. Body is raw ES DSL — `{"query": {"query_string": {"query": "…"}}}`, *not* `{"q": …}`, which 422s. |
| `POST /search` | Raw ES DSL. Returns **10,000 hits / ~20 MB every time**; see below. |
| `POST /query` | Returns HTTP 500 for every body shape tried. Treat as unavailable. |
| `GET /aggregates/{date}[/{attr}]` | Works, but see the silent-fallback trap below. |
| `GET /platforms`, `GET /labels` | Vocabularies. 372 platforms, 24 VLOPs. |

Statement fields (38 in total) include `platform_id`, `platform_name`,
`received_date`, `application_date`, `content_date`, `created_at`, `category`,
`source_type`, `decision_visibility_single`, `territorial_scope`, `content_type`,
`automated_detection`, `automated_decision`, `content_id_ean`, `puid`, `uuid`.

**Corrections to what this file previously recorded.** All three claims were
wrong, and each was wrong in the direction that would have shaped the design:

| Previously recorded | Measured |
|---|---|
| 1,000 rows max per query | **10,000** hits per `/search` |
| No pagination, `OFFSET` always 0 | **`search_after` paginates cleanly** — two consecutive pages, 0 overlap, strictly increasing ids |
| 5 MB response cap | **20.7 MB** observed on a single `/search` |

**`size` is ignored** — in the body *and* as a query parameter. Every `/search`
returns 10,000 hits. The only lever on response size is `_source`: restricting it
to the fields you need cut 20.7 MB to 3.4 MB, and to `["id"]` cut it to 1.0 MB.

**The silent-fallback trap.** `GET /aggregates/{date}/platform_name` returns
HTTP 200 with a date-only total and `attributes: {"1": "received_date"}` — it
ignores the unknown attribute rather than rejecting it. The valid attribute is
`platform_id`. A typo there costs coverage without ever failing, which is the
same failure mode as a wrongly-recorded `false` in a source entry. `/sql` answers
422 for a bad field, so `src/dsa.py` uses `/sql` and does not wrap `/aggregates`.

**`territorial_scope` cannot be used in SQL.** It is mapped as `text` with no
keyword sub-field, so `WHERE territorial_scope = 'DE'` is a 422. Country scoping
has to go through `/count` with a `query_string`. That split is why the collector
costs two SQL calls plus two counts per platform per day.

**Transient failures wear a client-error status.** `"No alive nodes. All the 1
nodes seem to be down."` arrives as HTTP **422**, not 5xx, so a retry policy
keyed on status alone treats it as permanent. Sustained querying also trips a
rate limiter that answers **429 with an HTML body**. Both are handled in
`DsaClient`.

**The real constraint is volume, not the API.** Over 2026-08-09..09-07:

| Platform | 30-day statements | DE-scoped | Article 16 notices |
|---|---:|---:|---:|
| Amazon Store | 196,927,242 | 69% | — |
| TikTok | 49,872,451 | 99% | 28,977 |
| AliExpress | 32,255,300 | 89% | 33,875 |
| Temu | 12,534,720 | 63% | 9,579 |
| Shein | 209,967 | 86% | 651 |
| Zalando | 195 | 100% | — |

**`territorial_scope: DE` is not a German filter.** It covers 63–99% of each
platform's output, because a pan-EU action lists all 27 member states. Nothing
narrows this source to Germany.

So item-level storage is off the table on volume grounds, and the monitoring
pattern stands — but **"track the trend, pull detail on a spike" does not work as
stated.** Temu's daily volume swings 9x with no underlying event (184k to 1.6M,
CV 0.68), so a spike alarm on raw volume is a false-alarm generator. The signal
is in composition and in the rare, human-originated slices.

Bulk daily dumps at `/explore-data/download-file/{id}/{full,light}` return **403**
to scripted requests, so the API is the route.

#### `received_date` is a submission date, not an event date

The same trap as `lastmod` (see CLAUDE.md), in a new place. `received_date` is
when the platform *filed*, and platforms batch:

- **Shein files in dumps.** 209,921 statements on 2026-08-21, 43 on 08-24, and
  **nothing on any other day** in that 20-day span. Its "~7,000/day" average is
  one dump divided by 30.
- **That dump was a back catalogue.** Its statements carry **221 distinct
  `application_date` values reaching back to 2024-02-26** — 2.5 years of
  moderation actions filed in one go.

So a daily zero is a normal result for a batch filer, not a collection failure,
and a large daily count may be an archive dump rather than a day of enforcement.
`src/dsa.py` therefore stores explicit zeros, and records each row's
`action_dates` (earliest, latest, distinct day count) so a dump is visible as
one. **Never present `received_date` to a customer as when something happened.**

#### What the collector stores

One `raw_item` per platform per day, `source_kind = 'dsa'`, `external_id` = the
date, `source_slug` = `dsa-temu` and so on. Payload carries the total, the
DE-scoped total, the Article 16 counts, the `action_dates` spread, and the full
breakdown by category and `source_type`. Platforms are configured in
`input/dsa_platforms.json`; Amazon and Zalando are present but disabled.

#### There is no item-level detail worth extracting — measured 2026-09-09

`SOURCE_ARTICLE_16` — third-party notices rather than the platform's own
automated sweeps — is three to four orders of magnitude rarer than the bulk and
looked like the place to find readable, client-relevant detail. It is not.
Sampling 10,000 statements per platform:

| Platform | Distinct explanation texts | Distinct `decision_facts` | Carrying a product EAN |
|---|---:|---:|---:|
| Temu | 25 | 9 | 0 |
| Temu, Article 16 only | 7 (one covers 74%) | 2 | 0 |
| TikTok | 173 | 4 | 0 |

Every record is a template — *"It is suspected that the product listings of your
store infringed intellectual property rights…"* — with **no product name, seller,
brand, or link**, and `content_id_ean` is never populated. The daily aggregate
already captures everything the item level holds, so the `search_after`
pagination that does work buys nothing here. Do not spend time extracting
statements.

**No carrier is in the database.** All 372 registered platforms were checked: no
parcel, logistics or courier company appears, J&T Express included. The
statement-of-reasons duty falls on online platforms, not carriers, so for the
client's own brand this source is a structural zero rather than a thin one.

So the source is a background metric, not a signal source. It supports a chart
and a sentence about how a platform's enforcement mix is shifting. For something
that *names* a Chinese-origin product sold in Germany, EU Safety Gate is the
source — see todo.md §2.

### Not machine-readable

| Source | Problem |
|---|---|
| **Zoll** | No RSS, and a sitemap containing only job vacancies. The press page does list ~11 recent releases directly, so it is scrapeable — but see below. |
| BMWK / BMDV | No usable sitemap or feed found |
| BAuA | Returns 403 to automated requests |
| EU competition case search | HTML only |

**Zoll: don't build it.** It looked like the important gap — German customs, a
cross-border parcel carrier — so it was worth checking what Zoll actually publishes
before investing in the scrape. Its press categories are Schwarzarbeitsbekämpfung,
Rauschgift, Waffen, Zigaretten and Sonstiges. Of 11 recent releases, 3 matched
parcel/e-commerce/China terms and two of those are false positives: a drug-detection
dog finding "Pakete", and a general traveller-information piece that happens to
mention postal consignments. Roughly 1 in 11 is marginally relevant.

Zoll publishes *local enforcement news*, not customs policy. Cross-border parcel
regulation — de-minimis reform, VAT on low-value imports, platform liability — comes
from the Commission (already covered by the Taxation & Customs feed) and the German
finance ministry. The hardest source to collect is also the least valuable one, and
the effort belongs elsewhere.

Checked directly: sweeping the configured sources for customs terms over 120 days
returns **178 articles**, so the topic reaches us without Zoll. The distribution
matters more than the total, though. The mainstream outlets' customs coverage is
mostly the US–Canada tariff dispute — real news, irrelevant to J&T. The on-target
material sits in the trade press:

```text
[Onlinehändler-News] Temu Steuern Zölle
[Onlinehändler-News] Paketabgabe soll Temu treffen
[Onlinehändler-News] Abschaffung Zollfreigrenze
[Onlinehändler-News] Kontrolle Importware — Zoll reagiert
[etailment]         EU-Zollabgabe; China importiert 62 Prozent
```

So the trade press covers Zoll's *decisions and their business consequences*, which
is what the client needs, while Zoll's own feed would have supplied drug seizures.
Dropping it improves the signal-to-noise ratio rather than costing coverage.

## Signal yield — measured 2026-09-09

Crawlability is not yield. This is a 90–120 day sweep of every source, matching
titles and URL slugs against the contract's brand terms (J&T, Temu, Shein,
AliExpress, TikTok Shop) and against sector terms (Paket, KEP, Zoll, E-Commerce,
Produktsicherheit, DSA, …).

**These are floors.** Title and slug matching cannot see a brand named only in
paragraph twelve, so true yield is higher — but the ranking between sources is
sound because every source is measured the same way.

| Source | Articles | Brand hits | Sector % |
|---|---:|---:|---:|
| etailment | 4000 | **165** | 6.7% |
| Onlinehändler-News | 404 | **16** | 19.3% |
| LOGISTIK HEUTE | 2730 | **11** | 6.8% |
| e-commerce Magazin | 156 | 4 | 28.2% |
| DER SPIEGEL | 3686 | 3 | 0.8% |
| DIE ZEIT | 4000 | 3 | 0.5% |
| FAZ | 3608 | 3 | 0.3% |
| VerkehrsRundschau | 880 | 2 | 3.0% |
| Händlerbund | 430 | 2 | 4.9% |
| Handelsblatt | 994 | 2 | 1.9% |
| WELT, WirtschaftsWoche, Tagesschau, t3n, BGL, BVDW, BVL, DSLV, HDE, Wettbewerbszentrale, bevh | — | **0** | ≤5% |

Three sources carry the news side: **etailment, Onlinehändler-News and LOGISTIK
HEUTE produce 192 of the 211 brand hits found.** The eight mainstream outlets
together produce 11. WELT and WirtschaftsWoche produced none in 120 days.

One measurement caveat: feed-only sources (Tagesschau, t3n, HDE) can only ever show
what is currently in the feed, so they cannot be backfilled and their totals are
not comparable.

## Paywalls — measured 2026-09-09

Tested by taking the *relevant* articles (the brand-matching ones above, not
whatever was newest) and reading each publisher's own schema.org
`isAccessibleForFree` flag plus extracted body length, with no login.

| Source | Checked | Declared free | Paid | Median words |
|---|---:|---:|---:|---:|
| DER SPIEGEL | 3 | 3 | 0 | 415 |
| DIE ZEIT | 3 | 3 | 0 | 595 |
| FAZ | 3 | 1 | **2** | 198 |
| Handelsblatt | 2 | 2 | 0 | 10 (video pages) |
| etailment | 5 | 5 | 0 | 584 |
| LOGISTIK HEUTE | 5 | 5 | 0 | 975 |
| Onlinehändler-News | 5 | unflagged | — | 256 |
| e-commerce Magazin | 4 | unflagged | — | 1002 |
| VerkehrsRundschau | 2 | unflagged | — | 452 |
| Händlerbund | 2 | unflagged | — | 1239 |

**Of 29 relevant articles checked, exactly 2 were paywalled — both at FAZ.** The
paywall on German news sites sits mostly on commentary and analysis; wire-style
coverage of Temu, Shein, customs and platform regulation is largely free, and full
body text was extractable anonymously nearly everywhere.

> **Amendment, 2026-09-09.** The table above sampled only *brand-matching* articles,
> which is the sample most likely to be unrepresentative — if a publisher gates its
> analysis and not its wire copy, brand hits will skew free. Re-checked on a broad
> sample of recent articles:
>
> - **etailment** — 0 paid of 14. Free.
> - **Onlinehändler-News** — 0 paid of 14, bodies 257–462 words with no stubs. Free.
> - **LOGISTIK HEUTE** — **partially paid.** `/news/` free in 17 of 17 (846–1,341
>   words); `/fachmagazin/fachartikel/` declares `isAccessibleForFree: false`. The
>   five brand hits in the table above were all `/news/`, which is why it read as
>   fully free.
>
> That decision was later changed: `/fachmagazin` is now excluded and its stored
> rows were purged. Public `/news` articles remain in the full-text tier.
>
> Method note: the string "abonnement" appears on **all 20** LOGISTIK HEUTE pages
> sampled — it is footer promo. A marker-based check would have condemned the entire
> site. Trust the publisher's `isAccessibleForFree`, not page text.

This inverts the earlier assumption. Subscriptions are not the blocker they looked
like, and the five missing login modules (Handelsblatt, WirtschaftsWoche, SZ, FAZ,
DVZ) are not on the critical path. FAZ is the only one with demonstrated paid
relevant content. Handelsblatt's brand hits were video pages carrying ~10 words of
text — its 994 articles produced nothing readable on topic.

The production backfill on 2026-09-10 added an important correction: **DVZ and t3n
are mixed-access**, with both successful public bodies and subscriber-only items;
e-commerce Magazin also produced declared-paywall results. “Paywalled site” is
therefore not a useful binary. Subscription decisions must be based on relevant,
paid-only, non-duplicated articles per month rather than publisher labels.

Sample sizes are small (2–5 per source). Re-measure before making a purchasing
decision, but the direction is consistent enough to stop treating paywalls as the
main obstacle.

## Open decisions

1. **Which regulatory sources are in scope.** The list above is a proposal, not a
   commitment. Safety Gate is built; EU presscorner and Bundeskartellamt are
   collectable through the regulatory article pipeline today.
2. **Continue systematic source audits.** Several sources now have evidence-based
   path restrictions. Apply new restrictions only after measuring what useful URLs
   they would remove, and record the decision in this report.
3. **Süddeutsche** — recheck once a subscription exists.
