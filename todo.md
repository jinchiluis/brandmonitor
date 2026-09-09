# todo.md — where the build stands and what comes next

Written 2026-09-09 at the end of a long session, so a later session can pick up
without replaying it. Findings live in [docs/source_coverage.md](docs/source_coverage.md);
this file is the plan and the open questions.

---

## Where things stand

**Working end to end**

| Piece | State |
|---|---|
| Vendored crawler wired in | `vendor/newscrawler/`, imports as `vendor.newscrawler.<module>` |
| `run.py probe` | Reports what a site supports, drafts its JSON entry |
| `run.py migrate` / `collect` / `status` | SQLite storage, per-source accounting, watermarks |
| News sources | 24 configured in `input/germany_medias.json`, all probe-verified |
| Regulatory sources | 8 configured in `input/regulatory_sources.json`, collection verified |
| Body collection | `src/bodies.py`; automatic after discovery plus `run.py fetch-bodies`; durable retries and content-based versions |
| Tests | 86 passing after body-stage implementation |

Last verified run: 8/8 regulatory sources ok, 879 items stored, re-run stored 0
(dedup holds).

**Deliberately stubbed**

- `src/assess.py` — raises `NotImplementedYet`. Storage and query logic are real;
  the prompt is not designed.
- No client profile exists yet (`clients/jt-express/profile.json`).
- No report generation.

**Explicitly out of scope**

- **客户投诉 / 服务质量 (customer complaints, service quality).** The JT document
  lists these under 极兔自身舆情监控, and news crawling cannot produce them — for a
  parcel carrier they live in reviews and comments (Trustpilot, Google, Reddit),
  which is a different crawl path with different extraction. Decided 2026-09-09:
  not in this build. The scope is regulatory and news signals. This needs saying in
  the SOW or the report so the first monthly delivery is not where it surfaces.

**Fixed 2026-09-09 after review**

| Bug | Fix |
|---|---|
| `source_kind` hardcoded `'news'`; all 879 regulatory rows mislabelled | Passed through from the run kind; `002_backfill_source_kind.sql` recovers existing rows from `run.kind` via `first_run_id`. Verified: 879 rows now `regulatory`, track is selectable. |
| One failing discovery method skipped the source's other methods, and any error discarded that source's already-collected hints | Methods run independently, errors are aggregated, partial results are stored |
| `content_hash` computed and never compared; `version` always 1, so a changed item was silently dropped | A changed hash now writes version *n+1* and keeps the prior row |
| Undated items starve under `ORDER BY published_at DESC LIMIT` | Orders by `COALESCE(published_at, fetched_at)` |
| `NEWS_LOOKBACK_DAYS` dead; collect defaulted to 7 while config said 30 | Default now reads the config value |
| An explicit `--days` window shorter than the gap since the last run advanced the watermark over the hole | Such a run collects but does not move the watermark, and says so |
| BNetzA config carried 2 of the 3 verified feeds | Third feed added; run went 27 → 67 found (77 before hint de-duplication; ten press releases appear in more than one of its three feeds) |
| The versioning fix above ratcheted: `store_hints` compared only the newest version, and `collect_source` returned one article once per discovery method. Copies differ (plain sitemaps carry no title, `lastmod` ≠ `pubDate`), so each looked changed relative to the other — **+2 rows per article per run, unbounded**, on the 8 news sources with `sitemap`+`feeds`. Latent: no regulatory source has two methods, and BNetzA's cross-feed duplicates are byte-identical, so the regulatory runs stayed clean. | `_dedupe_hints()` collapses hints by normalised URL before storage, keeping the richest copy; `store_hints` now compares against every stored version, not just the newest. Verified flat over 6 identical runs; a genuinely retitled article still versions. |
| No test covered `collect.py` | `tests/test_collect.py`: dedup, versioning, track separation, per-method error isolation |
| `discover_sitemaps` appended the same sitemap twice (www and non-www) when robots.txt declared none, doubling fetch cost and every "found" count | De-duplicated by content location. Verbraucherzentrale 1,532 → 766 found. Affected BVL, FAZ, LOGISTIK HEUTE, WELT, Verbraucherzentrale |

**Numbers to re-measure:** two of the fixes above cut "found" counts without
changing what is stored, so the recorded volumes are high in two independent ways.
The crawlability tables in `docs/source_coverage.md` and the volumes in each source
entry's `notes` are ~2× high for the five duplicate-sitemap sources (BVL, FAZ,
LOGISTIK HEUTE, WELT, Verbraucherzentrale). Separately, any source with more than
one discovery method double-counted articles found by both — measured at 13% for
BNetzA, and the 8 news sources with `sitemap`+`feeds` are affected but not yet
re-measured. The signal-yield table deduplicated before counting and is unaffected
by either.

---

## Decided 2026-09-09 — the storage tier map

Full text for every source was rejected not on storage cost (all 24 news sources
would be ~35 MB/month, ~416 MB/year — negligible) but on **publisher risk**:
systematically fetching ~1,300 articles a month from a paid subscription account is
what T&Cs prohibit and what gets accounts flagged. Title-only everywhere was
rejected because it makes recall unmeasurable and reports unquotable.

| Tier | Volume | Store | Filter axis |
|---|---:|---|---|
| Trade press + associations | ~2,400/mo | **full text** | brand |
| Regulators | ~340/mo after the VZ narrowing | **full text** | **topic** |
| Mainstream majors | ~10,000/mo | **title only**, fetch body on match | brand |

Rationale, measured:

- **The three high-yield trade sources — etailment, Onlinehändler-News, LOGISTIK
  HEUTE — produce 192 of 211 brand hits**, together ~2,380 fetches and ~143 MB/year.
  Full-texting them buys ~91% of measured signal.
  Paywall status, re-checked 2026-09-09 on a broad sample rather than only on
  brand-matching articles: etailment **0 paid** of 14 sampled, Onlinehändler-News
  **0 paid** of 14 (consistent 257–462 word bodies, no stubs), LOGISTIK HEUTE
  **partially paid** — `/news/` free in 17 of 17 sampled (846–1,341 words) but
  `/fachmagazin/fachartikel/` declares `isAccessibleForFree: false`. No config
  change needed: `_declares_paywall()` in `src/bodies.py` already marks such pages
  `unavailable` rather than storing them, so the magazine articles keep their titles
  as an index and never have their text taken. Do **not** narrow LOGISTIK HEUTE to
  `allowed_dirs: ["news"]` — `/fachmagazin` carries real signal (the customs sweep
  found `fachmagazin/fachartikel/markt-news-zoll-lufthansa-cargo-…`), and knowing an
  article exists beats not knowing.
  Measurement trap worth remembering: the string "abonnement" appears on **all 20**
  LOGISTIK HEUTE pages sampled — it is footer promo. Marker-based paywall detection
  would have condemned the whole site; the publisher's own `isAccessibleForFree` is
  the signal to trust.
- **Keep the majors' titles.** Sport/culture/entertainment is only **10%** of their
  volume; the bulk is ZEIT's dpa wire (78% of ZEIT) and WELT's `regionales` (53% of
  WELT). Titles cost ~10 MB/year and are the index that tells you which handful of
  articles to fetch. Dropping them would leave the majors dependent on Google News
  alone, whose zero results are ambiguous.
- **Do not aggressively section-filter the majors.** One of the three ZEIT Shein
  articles found sat at `zeit.de/news/2026-09/01/shein-startet-mit-deutlichem-minus`
  — inside the agency wire that looks like pure noise. The only cut worth
  considering is WELT `/regionales` (1,621 of 3,035), and that is reviewable rather
  than obvious.
- **News is filtered by brand; regulation is filtered by topic.** A de-minimis rule
  change matters even though it never says "Temu" or "J&T". A brand-keyword
  prefilter — right for news — would be actively wrong for the regulatory track.
  This is why regulators get full text: the press release *is* the content, and
  topical relevance cannot be judged from a headline.

Config gotcha found while measuring: **FAZ nests everything under `/aktuell/`**, so
depth-1 `allowed_dirs` is useless there and it needs two-segment paths like
`aktuell/wirtschaft`.

### Verbraucherzentrale — investigated, narrow rather than drop

766 of 929 monthly regulatory items. Its sitemap is the whole site: `/wissen` 1,832,
`/verbandsklagen` 862, `/urteile` 290, `/veranstaltungen` 285, `/bildung` 193,
`/rezepte` 118.

**`/wissen` is mostly not worth taking.** Only **110 of 1,832 (6%)** are
client-relevant by slug, and the relevant ones concentrate sharply:

| Subsection | URLs | Relevant |
|---|---:|---:|
| `wissen/digitale-welt/onlinehandel` | 38 | **100%** |
| `wissen/vertraege-reklamation/abzocke` | 19 | **100%** |
| `wissen/vertraege-reklamation/kundenrechte` | 117 | 15% |
| everything else under `/wissen` | ~1,650 | ~2% |

The rest is food (482), health (276), energy (137), insurance (244).

More important than the ratio: **`/wissen` is evergreen reference, not events.**
Sampled pages are permanent guides — "Meine Rechte beim Onlineshopping" (743 words,
no dates), "Maschen mit Vorschussbetrug" (1,131 words). They get re-touched in bulk
(421 pages in June, 330 in July, 320 in August), so the same guide would surface as
"changed" every few months forever. Monitoring wants events. The exception is
`/abzocke`, which carries *specific* fake-shop warnings naming individual sites —
those are genuine events, 19 URLs, 100% relevant.

**Applied 2026-09-09:**

```json
["verbandsklagen", "urteile", "wissen/vertraege-reklamation/abzocke"]
```

Class actions and court rulings are events; fake-shop warnings are events.
`wissen/digitale-welt/onlinehandel` is 100% on-topic but evergreen, so it was left
out — include it only if standing background guidance is wanted alongside events.

Effect, measured: Verbraucherzentrale **766 → 203** items per 30 days, and the
regulatory run as a whole 929 → 356. The 203 break down as `verbandsklagen` 184,
`urteile` 18, `abzocke` 1 — so class actions dominate and fake-shop warnings are
rare, which is worth remembering before expecting volume from `/abzocke`.

**Purged 2026-09-09.** The 563 rows the narrowed config would no longer collect were
deleted; 203 kept. Classification used the crawler's own `url_matches_dirs` rather
than a LIKE pattern, so what remains is exactly what a fresh sweep produces. No
assessments referenced them (the table was empty). Backup taken first at
`data/brandmonitor.pre-vz-purge.20260909_094425.sqlite3`.

Audit that preceded it, worth keeping in mind before any future wipe: the database
was in better shape than it looked — 919 rows, **all version 1**, correct
`source_kind`, no duplicates, no ratcheting residue. The bugs fixed that day
affected reported *counts*, not stored contents; the one that did corrupt data
(`source_kind`) was repaired by migration 002. Also verified: `src/bodies.py` queues
from `raw_item`, so already-stored rows can still get bodies without re-collection,
and at the time of the audit **86 of 86** feed-only items were still present in their
live feeds, so nothing was yet irreplaceable. That last property is temporary — EU
presscorner holds 50 items and publishes fast.

**Trap for any future wipe:** `watermark` holds `collection:regulatory`. Clearing
`raw_item` without also clearing the watermark means the next run collects from the
mark forward and leaves a permanent hole.

**Resolved for full-text sources by §0:** bulk `lastmod` changes now queue a body
recheck, without writing another raw version. A version is appended only when the
normalized title or body changes. Title-only sources retain their metadata-based
versioning. Existing historical versions are preserved.

### BVL — narrowed 2026-09-09, same story as Verbraucherzentrale

BVL is an association website, not a publisher, and its sitemap is the whole site.
Of 2,002 items collected: `/service` 319 (147 members-only), `/files` 304 (CV and
event-programme PDFs), `/lore` 273 (an academic journal's aims-and-scope and
copyright pages), `/en` 217 (English mirror of the same pages),
`/logistik-indikator` 117 (quarterly index pages), plus membership, contact,
donation and obituary pages. **Only 3 of 2,002 rows carried a title** — its sitemap
ships bare URLs.

Measured yield: **0 brand hits, 13 sector hits (0.6%)**, and most of those 13 are
tag pages (`/blog/tag/china/`) or chapter offices (`/china-beijing`).

It is not worthless, though. Buried in the blog:
`/blog/wie-das-ende-von-de-minimis-chinesische-plattformen-starkt` — "how the end of
de-minimis strengthens Chinese platforms", from the client's own sector association.

Applied: `allowed_dirs: ["blog", "presse"]`. Verified — **2,002 → 174 eligible**,
1,828 excluded; total body backlog 6,612 → 4,784. No purge needed:
`run_body_fetch` re-checks `url_matches_dirs` against current config when selecting
tasks, so excluded rows keep their titles and are simply never fetched.

**Re-audited 2026-09-09 — the narrowing was too generous and the dates are fake.**

The sitemap's 5,323 URLs all carry **one identical `lastmod`**,
`2026-09-06T00:17:17+00:00`, rewritten whenever the sitemap regenerates. So BVL's
dates carry no information at all, everything always looks fresh, and each
regeneration changes every discovery hash — which flips every BVL row in
`body_fetch` back to `pending`. Four such stamps are visible in the collected
history. On a daily schedule that is a permanent re-fetch treadmill.

The `["blog", "presse"]` narrowing kept 171 rows of which ~80% is furniture: 94
`/blog/tag|author|category` indexes, 32 `/presse/multimedia` event photo galleries
from 2021–2024, and the `/presse/rss` and contact pages. Real content is ~11 blog
posts and ~19 releases, several from `meldungen-2022/`.

Now `allowed_dirs: ["blog", "presse/meldungen"]`.

**Corrected 2026-09-09 (evening).** This section previously claimed the narrowing
plus the index-page filter gave "2,002 → ~30". It did not. The re-run stored
**2,304** BVL rows, because `is_furniture()` tested only `segments[0]` while
BVL nests its listings one level down (`/blog/tag/<term>`) — so it caught
**none** of them, despite its own comment describing exactly that case. BVL is
`content_mode: full_text`, so each one had also queued a body fetch: 2,280 of
the 4,969 pending fetches were BVL tag and author pages.

Fixed and purged. `is_furniture()` now matches an index marker in the **first two**
segments, plus `/page/<n>` pagination, `wp-content` assets and `?replytocom=`
comment permalinks. A separate `is_malformed()` drops URLs whose path carries
characters RFC 3986 forbids — BVL's sitemap appends a JavaScript fragment
(`/blog/tag/zoll/"%20+%20$(%20img%20)…`) to **816** of its URLs.

**Why the marker search stops at depth 1, which is load-bearing.** Searching
every segment also matches FAZ's
`/aktuell/feuilleton/buecher/autoren/<article-slug>` — a real content section
named `autoren` — and silently drops 21 genuine titled articles. A marker deep in
a path is a section name; near the front it is a listing. A dry run over all
18,092 stored URLs caught this before it shipped; without that check the filter
would have looked like it worked, because a wrongly-dropped article is
indistinguishable from a quiet source.

Result: BVL **2,304 → 629** (42 `presse/meldungen`, 584 blog posts, ~2 versions
each), body queue 5,405 → 3,731. Purge removed 1,675 rows, of which exactly one
carried a title — a `?replytocom=` duplicate whose canonical URL was kept. The
de-minimis post survives. Backup:
`data/brandmonitor.pre-furniture-purge.20260909_161417.sqlite3`. Watermarks were
deliberately left alone: these URLs are now filtered at collection time and will
not return, so clearing the mark would only refill a covered window.

### etailment — audited 2026-09-09, narrowed to /magazin

The highest-volume news source, and the numbers did not survive reading.

Of 2,000 stored rows, **519 are index pages** — `/tag` 442, `/autoren` 25,
`/themen` 24, `/experten` 11, `/format` 8, `/dossiers` 4, plus six `/magazin/<topic>`
category landings. They are separable exactly: all 519 share one identical
`published_at`, because `sitemap/0.xml` carries no `<lastmod>` on any of them and
the crawler substitutes fetch time. Fetching them confirms it — they have no
schema.org Article markup at all.

**0 of 2,000 rows carry a title.** The titles exist and are unused:
`news-sitemap.xml` is declared in the sitemap index and ships `<news:title>` plus a
real `<news:publication_date>` for a rolling 3-day window (~13 articles/day, which
is etailment's true output rate).

**A 2026-08-13 migration restamped the whole archive.** 5,659 of 6,046 sampled
sitemap entries share that one day, an article slugged `eoscar-2002` included.
Sampling 30 undated-slug articles for their real `datePublished`: **27 were
published 2024 or earlier**, back to 2001. The 2,000-row cap therefore pulled an
arbitrary slice of a 25-year archive, not recent news.

Yield is also thinner than the headline count: 93 brand-matching articles, but
**74 (80%) are `morning-briefing` link roundups** whose slug enumerates every
company mentioned — one row matches Temu, Shein, AliExpress and TikTok while being
about none of them. Roundups are 350 of 1,481 articles. **19 are genuinely about a
brand. J&T: 0.**

No paywall: 53 article pages sampled across random, undated and brand-matching
subsets, all `isAccessibleForFree: true`. robots.txt names `ClaudeBot` explicitly.

Applied: `allowed_dirs: ["magazin"]` — **2,000 → 1,485**, and the index-page filter
removes the rest.

**Resolved 2026-09-09 — news sitemaps are now traversed first.** The titles and
real dates were always there; the crawler never reached them. `fetch_sitemap_urls`
already parsed `<news:title>` and `<news:publication_date>`, but etailment lists
`news-sitemap.xml` last behind six archive files sharing one `lastmod`, so the
newest-first sort was a no-op and `max_per_source=2000` was spent on `0.xml` and
`1.xml` first. Sorting news sitemaps ahead of the rest fixes it: **0 → 40 titled
hints with real dates**, and at `max_per_source=50` all 40 arrive before any
archive entry. General, not per-source — it helps any site whose archive would
otherwise exhaust the cap. See vendor/PROVENANCE.md.

The 3-day window objection is answered by the daily cadence, which leaves two days
of slack — but it does mean **a missed run loses coverage permanently** for this
source. Worth a heartbeat check once collection is scheduled.

### Body fetcher pacing — fixed 2026-09-09

`run_body_fetch` is a serial loop with **no delay between requests**, and tasks sort
by `(attempted_at, source_slug, external_id)` — with nothing yet attempted that is
purely alphabetical by source. Hence all 86 fetches in the first body run went to
bevh.org and nothing else was touched.

Harmless in steady state (~157 items/day across 16 sources). It bites on the one-off
backlog: clearing 4,784 at `limit: 1000` marches alphabetically through ~503
requests and then sends roughly **500 consecutive requests to etailment.de with no
pause** — the source carrying 165 of the 211 measured brand hits, and the one least
affordable to be blocked by.

The discovery crawler does pace itself by comparison: `time.sleep(0.4)` between
title fetches in `crawl_site`, `0.1 + random*0.2` in `discover_sitemaps`.

Fixed: `interleave_by_source()` round-robins tasks across sources after the
retry-priority sort, and `pace_host()` enforces `body_fetch.delay_seconds` (1.0s)
**per host** rather than globally — interleaving already spaces domains apart, so a
blanket sleep would only make mixed batches slower without making them politer, and
the per-host rule still paces correctly when one source is alone at the tail of a
backlog. Verified on a real batch: 24 tasks spread across **16 sources**, 1-2 each,
where the previous run had sent all 100 to bevh.org.

Also unresolved from the same run: **`cmd_collect` exits non-zero when any body is
`failed` or `unavailable`.** But `unavailable` means "publisher declares a paywall",
the designed outcome for the majors — so once they start fetching, every scheduled
run reports failure forever and the exit code stops meaning anything. Individual
article outcomes are not run outcomes.

## 0. Body text — implemented 2026-09-09

`collect` now stores hints and then runs a bounded public-body fetch stage.
`fetch-bodies` can separately backfill old hints and retry failures after those
items leave the discovery window. Migration `003_body_fetch.sql` adds the durable
queue. Defaults: 100 attempts per invocation; the remaining backlog is reported.

- Source-level `content_mode`: 16 trade/association sources and 8 regulatory
  sources request full text; 8 mainstream sources remain title-only.
- Extracted text lives at `raw_item.payload.body_text`, with fetch provenance.
  Enrichment appends a version; unchanged rechecks do not. Existing hints and
  assessments stay intact.
- Public HTTP plus trafilatura; paywalls, missing pages and unsupported media are
  explicitly unavailable. HTTP/extraction failures retry on the next batch.
- Changed discovery hints trigger rechecks. `--refresh` also checks successful
  bodies when the publisher has not signalled a change.
- Backfill respects the applied Verbraucherzentrale sitemap section restriction.

Verified on four live article pages (etailment, LOGISTIK HEUTE, EDPB, vzbv) in a
separate validation database. Repeating attempted zero fetches; forced refresh
stored zero new versions. The live check also caught a homepage and a paywalled
page, and prompted a fix to preserve quotations inside slider wrappers.

Usage and storage semantics: [docs/body_collection.md](docs/body_collection.md).

**Escalation ladder added 2026-09-09 (evening).** The stage was a single
unauthenticated request; it is now four rungs — plain HTTP, PDF text layer
(pypdf, BSD-3), headless browser, subscriber login — each firing only when the
rung below failed in the one way it can fix. Verified live: a BNetzA Amtsblatt
PDF yields 6,278 words and EU presscorner 807 via the browser, a source that was
38/38 dark. The vendored `fetch_html` is deliberately not the entry point; the
reasons are in [docs/body_collection.md](docs/body_collection.md).

Rung 4 is wired but inert — no credentials in `.env`, and `paywalls.json` covers
SPIEGEL, ZEIT and WELT but **not FAZ, Handelsblatt, Süddeutsche or WiWo**, so four
of the seven paywalled majors have no login path at all.

Remaining: run the real archive backfill in batches, sample extraction quality
across the other sources, then measure prefilter recall once the customer confirms
brands/topics.

## 1a. Client profile and prefilter — implemented 2026-09-09 (evening)

The prefilter is built and is now client-driven. `src/news_selector.py` holds no
brands of its own; the rules live in `clients/jt-express/profile.json` and load
through `src/profile.py`. Adding a customer is a directory, not a code change,
which is what the "config and prompts, not another scraper" rule asks for.

- **Profiles are self-contained on purpose.** A shared topic file would let an
  edit change a client's assessments without changing their `profile_version`,
  breaking the traceability rule. Duplicating ~40 lines per client is the cheaper
  side of that trade; revisit with a resolved-profile hash at five-plus clients.
- **Terms, not regex, by default.** A trailing `*` allows a suffix, which German
  needs (`zollfreigrenz*` → Zollfreigrenzen); spaces and hyphens inside a term are
  interchangeable. A `pattern` escape hatch covers what terms cannot express:
  `J&T` (ampersand and spacing variants), `极兔` (`boundaries: false`, because `\b`
  never matches CJK), and `import` (must not match *important*).
- **Selection now reads the body where one is stored**, not just title and slug.
- **The title and the body are held to different bars**, which is the change that
  made body matching worth having. See below.

### OR on the title, co-occurrence in the body

A rule means less in 1,500 words than in a headline. Matching bodies on plain OR
selected 133 extra items, and reading them showed **87% were keyword-only noise**:

- `t3n` "Für wen sich das iPad Air lohnt" matched **customs** on
  "13-**Zoll**-Display" — *Zoll* is German for both customs and **inch**.
- `vzbv` "Roaming" matched **parcel** on "Internet und Router im **Paket**" —
  *Paket* is also a **bundle**.
- `VerkehrsRundschau` "Nachrichten" ×3 matched **KEP** on the site's navigation
  menu, because those index pages have a nav list for a body.

So the body now requires **one brand or topic, or at least two broad keywords**
(`body_min_keywords`, in the profile). The title and slug keep plain OR.

Measured over the 342 stored bodies:

| Variant | Selected | Brand/topic finds kept |
|---|---:|---:|
| any single rule anywhere | 156 | 23 |
| body needs ≥2 of anything | 76 | 22 |
| **1 brand/topic, or ≥2 keywords** | **77** | **23** |

Tier-awareness is what makes the third row better than the second: a plain
"body needs two" drops a real bevh GPSR find where the topic was the only match.
A brand or a narrow topic stands alone wherever it appears — finding "Temu" in
paragraph twelve is the whole point of reading bodies.

Body-only finds fell 133 → 54, and every named noise case above is gone. Of what
remains, 26% name a topic and 6% a brand; the rest are plausible sector items for
the LLM to judge.

**The keyword tier stays high-recall on titles deliberately** — see §1b. A cheap
LLM judges titles, so the deterministic pass there only has to avoid missing
things, not avoid noise. Bodies get the stricter rule because they skip the cheap
stage entirely and go straight to the expensive one.

### The recall question — measured once, needs redoing after the backfill

Selecting on a headline has a recall cost: a brand can appear in paragraph twelve
and never in the title. Using the body removes that cost entirely for the 23
`full_text` sources and confines it to the nine `title_only` ones, where a miss is
unrecoverable because the body is never fetched.

First measurement, over the corpus as it stands:

| | |
|---|---:|
| eligible items | 15,264 |
| selected on title/slug only | 499 |
| selected body-aware | 632 |
| **found only via the body** | **133 (21%)** |

**Do not read that 21% as the recall cost.** Two caveats, and both matter:

1. Only **342 items have a body**, so the comparison differs on 2% of the corpus.
   Re-run it after the body backfill.
2. Of the 133 body-only finds, **3 name a brand** — and two of those three are
   junk (a Wettbewerbszentrale category page, a bevh member-directory entry). The
   rest are sector *keywords* appearing deep in unrelated text, which is noise
   rather than signal.

So the honest reading is that body matching is free and strictly better, but the
recall risk on the title-only tier looks **small**, not catastrophic — which
supports leaving it measured-but-unfixed for now.

The tool for the measurement exists: `--no-bodies` forces title/slug matching, so
running the selector both ways over the full-text corpus after the backfill gives
the number directly. That is the proxy for the title-only tier; it is probably
conservative, since the majors write longer pieces where a brand is likelier to be
buried.

Sharpening it further: the `title_only` tier is really **slug-only**. ZEIT ships a
title for 21 of 3,753 URLs and SPIEGEL for 215 of 1,806, so those sources are
matched on the URL slug alone. Their recall rests on publishers naming the brand
in the URL.

Still open here: `run_body_fetch` only selects `full_text` sources, so a selected
title-only item still cannot be fetched. That is the fetch-on-match gap.

## 1b. The assessment pipeline — shape agreed, not yet built

Decided 2026-09-09. Four stages, each cheaper per item than the one after it, and
each passing forward less. The asymmetry between titles and bodies runs through
the whole thing:

```text
  title_only tier                       full_text tier
  (9 sources, ~11,800 rows)             (23 sources, bodies stored)
        |                                       |
  news_selector (OR, high recall)               |
        |                                       |
  CHEAP LLM - judges the title only             |
        |                                       |
  run_body_fetch on what survives  ------> new bodies
        |                                       |
        +---------------+-----------------------+
                        |
             EXPENSIVE LLM assessment
             (new bodies + already-stored bodies)
```

Why it is shaped that way:

- **The cheap LLM only ever sees titles.** That is what lets the deterministic
  selector run wide open on titles: its job there is not to miss, and precision is
  bought later for very little. A keyword-only title match is fine because
  something cheap reads it before anything expensive does.
- **Bodies never enter the cheap stage.** A body is already the evidence, so
  paying twice to read it is waste — full_text items go straight to the expensive
  assessment. This is why the body tier is held to the stricter co-occurrence rule
  in §1a: nothing downstream will filter it.
- **Regulatory skips the selector entirely.** Filtered by topic, not brand, and
  relevance cannot be judged from a headline — so it goes straight to the
  expensive assessment. ~340 items a month, which is affordable.
- **The two streams merge before the expensive pass**, so the assessment sees one
  corpus and does not care how an item arrived.

### What has to exist first

1. **Fetch-on-match.** The blocking piece. `run_body_fetch` selects only
   `content_mode == "full_text"` sources, so a title-only item that survives the
   cheap LLM still cannot be fetched. Needs a way to queue an explicit set of
   URLs and a fetcher that honours it regardless of tier. `queue_body()` already
   exists and `fetch_body` already recovers the title, so this is plumbing rather
   than new mechanism. Note it also unlocks BVL, which is title_only since
   2026-09-09 and whose three selector-matched articles are currently
   identified-but-unfetchable.
2. **The cheap-LLM stage itself** — model, prompt, and what it returns. A verdict
   plus a reason is probably enough; it is a gate, not an analysis.
3. **The expensive assessment** — still §1, still waiting on the report sketch,
   because the output schema is driven by what the report carries.

### Open

- **Paywalls will bite here.** The cheap LLM will select ZEIT, WELT and SPIEGEL
  articles that fetch-on-match then cannot retrieve: the vendored login modules
  exist for those three, but no credentials do, and FAZ, Handelsblatt,
  Süddeutsche and WiWo have no login module at all. Worth knowing the hit rate
  before deciding whether subscriptions are worth buying — and note that
  fetch-on-match is the *defensible* usage pattern, a handful a week rather than
  the bulk fetching the tier design rejected.
- **Cheap-stage volume** is roughly the selector's title-tier output per cycle.
  Measure it once the backfill is in, since it sets the running cost.

## 1. Assessment layer — needs its own session

The biggest open piece, and the one most damaged by guessing. Settled already:
assessments are per client, carry `prompt_version` and `profile_version`, and have
their own watermark. `src/assess.py` has the storage and "what is unassessed" query
written; only the thinking is missing.

Decide, in roughly this order — §0 is implemented; step 1 still needs a collected
body sample and customer-confirmed matching terms:

1. **Prefilter before the LLM — built, see §1a.** Client-driven, plain OR, and
   body-aware where a body exists. Recall cost measured once and looks small;
   re-measure after the backfill with `--no-bodies`.
2. **One call or two.** Relevance and severity/sentiment may want separate prompts
   with different models; the reference repo splits assessment across layers.
3. **Model.** Reference repo uses Doubao. Not yet decided here.
4. **Output schema.** Driven by what the report needs — so sketch the report first.
5. **Profile versioning.** What happens to prior assessments when a brand or alias
   is added mid-cycle. The schema allows re-assessment at a new version; the policy
   for when to trigger it is undecided.

Reference material: `c:\apps\NewsCrawler\src\agents\` has working assessment agents
and prompt-assembly code from the previous generation.

## 2. Safety Gate collector — deferred, highest yield

Free, official, no credentials, and measured as the single best regulatory source:
over 12 weeks, 680 alerts, 471 of Chinese origin, **53 both German-notified and
Chinese-origin (~4.4/week)**, with `onlineTrader` naming Temu 26 times and Shein 21.
For comparison, `"J&T Express"` returned **zero** German news mentions in 30 days.

- Endpoint: `https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en`
- Each `<weeklyReport>` carries a detail URL; the detail document repeats
  `<notifications>` once per alert.
- Fields: `caseNumber`, `category`, `product`, `brand`, `name`, `barcode`,
  `riskType`, `danger`, `measures`, `URLrecall`, `notifyingCountry`,
  `countryOfOrigin`, `type`, `level`, `pictures`, `onlineTrader`.
- Client filter: `notifyingCountry` contains Germany **and** `countryOfOrigin`
  contains China; `onlineTrader` matched against the platform list.
- 1,114 weekly reports back to 2022, so backfill works.

Goes in `src/` as a new collector, not `vendor/` — it is our code. Writes into
`raw_item` with `source_kind = 'safety_gate'` and the alert JSON as payload.
Deliberately **not** forced through `ArticleRecord`: that would discard the fields
that make it valuable.

## 3. DSA Transparency Database — built 2026-09-09, and worth less than it looked

Token is in `.env` as `DSA_KEY`. Implemented as `src/dsa.py` and
`python run.py collect-dsa`; 18 tests in `tests/test_dsa.py`. Verified live:
4 platforms x 4 days stored, rerun stored 0, resume from watermark reports "up
to date", and a mid-window API failure stopped the watermark before the hole and
refilled it on the next run.

**Read this section before planning any report around this source.** It was
listed as "the highest-value platform-regulation source". Measurement does not
support that. It is a background metric, not a signal source.

### What it gives, exactly

Every VLOP files a statement of reasons each time it moderates something. The
database is a record of **platforms policing themselves** — not news, not
complaints, not enforcement by authorities. Three things come out of it, all
aggregate:

1. **Enforcement volume and mix, per platform per day.** Temu on 2026-09-08:
   595,910 filings, 69% unsafe/prohibited products, 27% IP infringement, 0.7%
   consumer information. Mix shifting over weeks is a defensible observation.
2. **Third-party-reported share.** `SOURCE_ARTICLE_16` means someone outside
   complained — Temu 9,579 against 12.5M total over 30 days. A rise means
   outside pressure is building.
3. **Compliance-posture events.** Shein dumping 209,921 statements in one day
   covering actions back to 2024-02 is itself the story, and is visible nowhere
   else.

### What it does not give — measured, not assumed

**There is no item-level detail to drill into.** This was the open question and
it is now closed. Across 10,000 sampled Temu statements:

| | Result |
|---|---:|
| Distinct explanation texts | **25** (Article 16 subset: **7**, one covering 74%) |
| Distinct `decision_facts` texts | **9** |
| Statements carrying a product EAN | **0 of 10,000** |

Every record is a template — *"It is suspected that the product listings of your
store infringed intellectual property rights…"* — with no product name, seller,
brand or link. TikTok is only slightly richer (173 templates in 10,000). **The
daily aggregate already captures everything the item level holds**, so the
`search_after` pagination that turns out to work buys nothing here. Do not spend
time extracting statements.

**It says nothing about J&T Express.** All 372 registered platforms were checked:
no carrier, parcel or logistics company appears. The statement-of-reasons duty
falls on online platforms, not carriers. For the client's own brand this source
is a structural zero, not a thin one.

**It is not German.** `territorial_scope: DE` covers 63-99% of each platform's
output, because a pan-EU action lists all 27 member states. Nothing narrows this
source to Germany.

**`received_date` is a submission date, not an event date.** Shein files in dumps
(209,921 on one day, nothing on most others) and one such dump carried 221
distinct `application_date` values back to 2024-02-26. Each stored row records
its `action_dates` spread so a back-catalogue dump is visible as one. Never show
`received_date` to a customer as when something happened.

**A spike alarm on raw daily volume would be a false-alarm generator.** Temu's
daily count swings 9x (184k-1.6M, CV 0.68) with no underlying event.

### Verdict

Keep it — it costs ~13 API calls and 4 rows a day and it is the only quantitative
series in the pipeline. It can carry one chart and a sentence in a weekly report
("Temu's unsafe-product removals rose from X% to Y% of its filings; third-party
notices up 40%"). It cannot produce an item, name a product, or trigger an alert.
**Do not build the report around it**, and do not let it displace §2.

### API corrections

The design note this file previously carried was wrong on every count: the limit
is 10,000 rows (not 1,000), `search_after` paginates cleanly (recorded as
impossible), and a `/search` response is ~20 MB (not 5 MB). `size` is ignored
entirely. `/aggregates` silently accepts an invalid attribute and returns a
date-only total — HTTP 200, no error — so it is not used; `/sql` 422s on a bad
field instead. `territorial_scope` cannot appear in SQL at all (mapped `text`),
so country scoping goes through `/count`. Transient backend failures arrive as
**422, not 5xx**, and sustained querying trips a rate limiter returning **429 with
an HTML body**. Full endpoint table in
[docs/source_coverage.md](docs/source_coverage.md).

### Licence

The data is **CC BY 4.0** — commercial reuse permitted with attribution, so any
client deliverable built on it must carry:

> European Commission-DG CONNECT, "Digital Services Act Transparency Database",
> Directorate-General for Communications Networks, Content and Technology, 2023

The attribution string ships in every stored payload, so a report cannot omit it
by forgetting where the number came from.

### Remaining

- **Amazon and Zalando are configured but disabled** in
  `input/dsa_platforms.json` — enable if a client wants the comparator. Amazon is
  the largest filer in the database (197M in 30 days).
- **The 30-day backfill has not been run into the primary database.** All
  verification used a scratch DB. `python run.py collect-dsa` fills it.

## 4a. Systematic source audit — the highest-value remaining work

Every source examined closely has changed the configuration: Verbraucherzentrale
766→203, BVL 2002→174, LOGISTIK HEUTE found partially paywalled, Zoll dropped, BPEX
was the wrong company, DVZ 13→73 after a crawler fix, bevh unfetchable. Seven for
seven. The remaining ~24 sources have not had the same scrutiny.

The method, the traps it has already caught, and a ranked list of where the leverage
probably is: [docs/source_audit_prompt.md](docs/source_audit_prompt.md). Most of it
now runs off the ~14,000 stored rows rather than re-fetching.

## 4. Source list follow-ups

- **Re-measure DVZ.** Its yield number (36 articles/90d) predates the sitemap
  pruning fix and is unreliable. Post-fix it returned 73 in 14 days.
- **DVZ `news-sitemap.xml`** is not declared in robots.txt, and `discover_sitemaps`
  only tries guessed paths when robots declares none. Merging guesses with declared
  sitemaps would find it, at the cost of extra requests on every site. Decision
  pending.
- **EU Commission press corner needs topic filtering.** The feed carries all
  Commission output (Ukraine, education); 0 of 50 items were relevant. Check
  whether the presscorner API accepts a topic or policy-area parameter.
- **Verbraucherzentrale is narrowed.** Review the yield of the retained event
  sections; old out-of-scope rows remain as history and are excluded from body backfill.
- **Feed-only sources cannot be backfilled** (Tagesschau, t3n, HDE, and most
  regulatory sources). A feed shows only its current window, so their history is
  whatever we captured. Start collecting early; the archive begins now.
- **Süddeutsche** is frontpage-only. Recheck once a subscription exists.
- **Zoll: dropped, deliberately.** Verified: it publishes local enforcement news
  (drugs, weapons, illegal employment), not customs policy. Customs signal reaches
  us through the trade press instead — 178 customs articles across the configured
  sources in 120 days, with the on-target ones ("Paketabgabe soll Temu treffen",
  "Abschaffung Zollfreigrenze") coming from Onlinehändler-News and etailment.

## 4b. Publication dates — partly fixed 2026-09-09, two gaps left

`lastmod` is a change signal, not a publication date. `src/bodies.py` now reads the
page's own `datePublished` during body fetch and records `published_at_source` as
`page` or `discovery`, so an item's age is traceable to where the date came from.
See CLAUDE.md for the rule and the BVL/etailment evidence.

- **`title_only` sources never get a real date.** The page date arrives with the
  body, and ZEIT, WELT, SPIEGEL, FAZ, Handelsblatt, WiWo and Süddeutsche are not
  body-fetched, so they keep whatever discovery gave them and stay
  `published_at_source = discovery`. All measured healthy today (1–2 day lag,
  ~1000 distinct timestamps per 1000 rows), so this is not urgent — but nothing
  would catch it if one of them started restamping. The gate lands when a source is
  scraped; decide what to do for those that never are.
- **The weekly report must flag undated items for human check.** 413 rows carry no
  date at all: Süddeutsche 296 (frontpage discovery), Bundesnetzagentur 67 (feeds
  without dates), BPEX 50 (frontpage fallback). They cannot be placed in a week and
  cannot be gated on recency. Excluding them silently drops BNetzA's parcel
  regulation output, which is real regulatory signal — so surface them in a flagged
  section rather than dropping or silently including them.

## 5. Things that will bite later

- **Bundestag DIP API key expires end of May 2027** — hardcoded in
  `c:\apps\NewsCrawler\src\crawler_gov\bundestag.py`, if that client is adopted.
- **Sweeping must run at least every 48h.** News sitemaps carry titles for ~48h;
  older entries come from plain sitemaps with no title and need a page-open each.
  Miss the window and a cheap sweep silently becomes an expensive one.
- **Eleven of 24 news sources produced zero brand hits in 120 days.** Fine as
  coverage, but the report must say "monitored, nothing found" rather than implying
  those outlets discussed the client.
- **`.env` holds the DSA token (`DSA_KEY`) and the dead Google Search keys.**
  Paywall logins and LLM keys still need adding.

---

## Suggested order for the next session

1. **Safety Gate collector.** No prompt dependency, no body-text dependency, and
   the only source reliably producing client-relevant items — so it is the shortest
   path to a report containing something. §2 has the full spec. Note that DSA
   (§3, now built) is aggregates only: it can say Temu's unsafe-product filings
   rose, but it names no product. Safety Gate is still the one that names things.
2. **Backfill bodies and review extraction** (§0 is implemented). Run bounded
   batches and inspect representative source text before measuring relevance.
3. **Verbraucherzentrale is already narrowed.** The body backfill respects that
   restriction; old rows outside those sections remain historical hints.
4. **Sketch the report output**, then design the assessment schema against it, then
   measure prefilter recall — in that order, since the report determines the schema
   and §0 makes the measurement possible.
5. Create `clients/jt-express/profile.json` **after the customer conversation**.
   The JT document suggests J&T Express, J&T International, Temu, SHEIN and TikTok
   Shop, but the actual brands and regulatory interests are not confirmed yet.

The complete 24-source news collection has not been run into the primary database.
A two-source news collection was verified in the isolated body-stage validation
database. The full archive and its extraction coverage still need measuring.
