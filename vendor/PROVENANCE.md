# Vendor Provenance

This file records the origin of committed code under `vendor/` and material changes
made after copying it. Vendored code is maintained as part of brandmonitor; it is not
kept synchronized with upstream.

## newscrawler

| Field | Value |
|---|---|
| Source repository | `https://github.com/jinchiluis/germany_risk_monitor` |
| Source path | `src/crawler_news/` |
| Source commit | `6a861151c64ae7d36e48561bec72b678370ce067` (`6a86115`, `origin/master`) |
| Commit date | 2026-06-13 |
| Copied on | 2026-08-18 |

The archive was taken from `origin/master`, not from the local working tree at
`c:\apps\NewsCrawler`.

### Included

```text
crawler.py
crawler_brightdata.py
crawler_google_feeds.py
crawler_html_utils.py
crawler_playwright.py
parallel_crawler.py
scraper.py
scraper_fetch_html.py
source_loader.py
paywall/handler.py
paywall/paywalls.json
paywall/{bild,manager_magazin,spiegel,welt,zeit}_login.py
```

The list records what was copied. Four of these files — `crawler_google_feeds.py`,
`parallel_crawler.py`, `scraper.py` and `scraper_fetch_html.py` — were deleted on
2026-09-16 as unreachable; see the dated entry below.

`crawler_brightdata.py` is kept because `crawler_html_utils.fetch_html` imports it
lazily when a source carries `"brightdata": true`, and it may support
social-platform collection. No source sets that flag today, and it is not the
default path for website fetching on the laptop.

### Excluded

- `scraped_source_reader.py`: unused by the copied modules
- `states/`: runtime cookies, watermarks, and downloaded data
- `__pycache__/`: generated files

### Integration requirements

Met on 2026-09-08. `src/config.py` supplies `CRAWLER_VERBOSE` and `src/logger.py`
supplies `get_logger`, which is all the copied modules import from the host. The
stale `src.crawler_news.*` imports are gone.

### Local changes

**2026-09-08 — flattened the package and rewrote host imports.** Files moved from
`vendor/newscrawler/src/crawler_news/` to `vendor/newscrawler/` directly, so the
tree matches the layout in `CLAUDE.md` and there is only one `src/` in the
repository. Every `from src.crawler_news.X import Y` became the relative
`from .X import Y`; imports of `src.config` and `src.logger` are unchanged and now
resolve to brandmonitor's own modules. Import path is
`vendor.newscrawler.<module>`, with `__init__.py` added at each level. No crawling
logic was touched.

Known stale comment: `collect_from_sitemaps` in `crawler.py` says `allowed_dirs` is
"used for RSS/frontpage, not sitemaps", but the function does apply it to sitemap
results. The behaviour is correct; the comment is not.

**2026-09-09 — de-duplicated sitemap discovery.** `discover_sitemaps` returns a
list de-duplicated by content location (host normalised for `www.`). When
robots.txt declares no sitemap, the guess loop tries every candidate path against
both the www and non-www base and appended each hit separately; both normally
serve the same file, so every URL on such a site was discovered twice. The
duplicates were only collapsed at storage, so fetch cost doubled and every
reported "found" count was inflated 2×.

verbraucherzentrale.de made it visible: 1,532 in-window hints collapsing to 766
stored, exactly 2:1. After the fix, 766 found and 766 stored. Five configured
sources declare no sitemap in robots.txt and were affected — BVL, FAZ, LOGISTIK
HEUTE, WELT and Verbraucherzentrale.

**2026-09-09 — `feed_urls`: explicit feeds per source.** `get_site_rules` now
returns a `feed_urls` list, and `collect_from_feeds` uses it in preference to
autodiscovery and `COMMON_FEED_PATHS` when set. Autodiscovery is skipped entirely
for a source that configures one, so a site can no longer be pointed at the wrong
feed.

Needed for the regulatory sources, where three of seven were unreachable:
`taxation-customs.ec.europa.eu/node/2/rss_en` and the Commission press corner's
`presscorner/api/rss?language=en` are neither advertised nor conventional, and
bundesnetzagentur.de advertises several feeds of which autodiscovery picked the
energy-auction one — 100 articles of Gasversorgung and Solar-Gebotstermine instead
of the press releases. That failure is worse than an empty result because it looks
like success. After the change: EU customs 0 → 30, press corner 0 → 50, BNetzA 100
wrong → 27 right.

**2026-09-09 — stopped pruning sitemaps on a CMS `lastmod`.** In
`collect_from_sitemaps`, an entry in a sitemap index was skipped when
`sm_lastmod or _sitemap_date_hint(sm)` predated the window. That preferred the
unreliable signal: a date embedded in the sitemap's own URL
(`sitemap-2019-03.xml`) is a deliberate archive marker, while `<lastmod>` is CMS
metadata that can be badly stale on a live sitemap. Pruning now uses the URL hint
only; `lastmod` is still used to date individual entries and to order traversal.

dvz.de is the case that exposed it. Its TYPO3 index reports
`lastmod=2024-07-08` for news sitemaps whose entries are current — the pruned file
holds 1000 URLs with 334 from June 2026, 332 from July and 61 from August. DVZ was
yielding 13 articles per fortnight instead of 73, roughly 1 in 6 of its real
output, with no error anywhere to show for it. Traversal cost stays bounded by
`max_sitemap_fetches` (100) and the existing newest-first ordering.

**2026-09-11 — configured additive sitemap roots.** `extra_sitemap_urls` supplements
the roots declared in `robots.txt` without enabling common-path probes for every
source. DVZ needs it: its declared sitemap yielded 10 URLs in a live 14-day probe,
while the undeclared `news-sitemap.xml` held 32 titled, publication-dated articles
from the latest two days. Top-level news roots are traversed before general roots so
an archive index cannot delay them or spend the fetch budget first. DVZ is now
`title_only`, so this restored discovery does not become a bulk paid-body crawl;
selected articles will use fetch-on-match.

**2026-09-11 — a hint records which field dated it.** `ArticleHint` gained
`date_source` (`news_sitemap`, `lastmod`, `feed`, `frontpage`, or None), defaulted
so existing constructions are unchanged. `fetch_sitemap_urls` returns a fourth
tuple element saying whether `<news:publication_date>` or `<lastmod>` supplied
the entry date; an entry that inherits the containing sitemap's `lastmod` or
filename month is labelled `lastmod`. `collect_from_feeds`, the frontpage
collector and the Google-feeds collector set the label at their constructors.

Needed because `pub_dt or lastmod` had collapsed two dates of opposite
trustworthiness into one field: `<news:publication_date>` is when the article
appeared, `<lastmod>` is when the URL changed and is restamped in bulk (BVL,
etailment). Nothing downstream could tell them apart, so a report could not know
which stored dates it may print. `src/collect.py` stores the label as
`published_at_source` on every row.

**2026-09-09 — traverse news sitemaps first.** `collect_from_sitemaps` sorted
nested sitemaps newest-first; it now sorts news sitemaps ahead of everything else
and applies the date order within each group, via a new `_is_news_sitemap()` that
matches on the filename rather than the whole URL. No parsing changed —
`fetch_sitemap_urls` already read `<news:title>` and `<news:publication_date>` and
preferred the news date over `lastmod`.

Ordering was the whole problem. etailment's index lists `news-sitemap.xml` last,
behind six archive files that all share one `lastmod`, so the sort was a no-op and
traversal ran in file order: `0.xml` (522 entries) then `1.xml` (5,000) exhausted
`max_per_source=2000` before reaching it. The result was 2,000 stored rows with
**no title at all** and a `lastmod` restamped to 2026-08-13 by a site migration,
of which ~90% were published 2024 or earlier.

Measured on etailment after the change: 0 → 40 titled hints carrying real
publication dates, and with `max_per_source=50` all 40 arrive before any archive
entry. The gain gets larger as a source's archive grows, since that is exactly
when the cap runs out early.

**2026-09-08 — feed autodiscovery in `collect_from_feeds`.** Added
`discover_declared_feeds()`, which reads the homepage's
`<link rel="alternate">` tags; `collect_from_feeds` now tries those before falling
back to `COMMON_FEED_PATHS`. Costs one homepage fetch per site and returns `[]` on
any failure, so a site that will not load cannot break the crawl.

Probing the client's first source list showed the fixed path list was silently
wrong about three of 24 sources. Tagesschau publishes at `index~rss2.xml`, HDE at
the Joomla `?format=feed&type=rss`, e-commerce Magazin at `/rss/news.xml` — none in
`COMMON_FEED_PATHS`, so all three recorded as having no feeds. After the change:
Tagesschau 0 → 143 entries, e-commerce Magazin 40 → 120, BGL 20 → 30, HDE 0 → 8.
Extending the path list would have fixed these three; autodiscovery fixes the next
site too, and a wrong `"feeds": false` costs coverage silently.

**2026-09-15 — a sitemap URL counts once against `max_per_source`.**
`collect_from_sitemaps` counted every in-window `<url>` entry, so a URL listed by
the news sitemap and again by an archive sitemap spent two slots. It now keeps one
hint per normalised URL, replacing the kept copy only with a richer one (a title,
then a date - the rule `src/collect.py`'s `_dedupe_hints` applies), and counts
only new URLs.

Found while widening the collection window by a 48-hour overlap (weaknesses.md
N1): WELT lists 111 URLs as 307 entries in two hours, and a 50-hour window hit the
2,000 cap at about 1,000 distinct URLs, dropping one of the latest two hours'
articles. After the change the same window yields 1,223 distinct URLs with none of
the recent ones missing. Nothing downstream changes, since storage already
de-duplicated; the reported "found" counts for such sources fall to distinct URLs.

**2026-09-15 — frontpage hints carry the link's headline.** `collect_from_frontpage`
built every hint with `title=None`, so all SZ items (frontpage-only) reached the
gates and the report as URL slugs. It now takes, per URL, a heading (h1-h4) inside
the link or inside the teaser the link names via `aria-labelledby`, then an element
with class `title` or `headline`, and only then the link's own text, capped at 150
characters; `<style>`/`<script>` text is ignored. The brief's "longest link text"
was measured first and rejected: SZ's article links are empty overlays, and its
card links wrap kicker, headline and teaser (median 252 characters, some with CSS).
On the SZ homepage 269 of 300 probe hints now carry a title, median 44 characters;
the untitled rest are section pages. A title-only item gains the title as one new
version (`src/collect.py` `_is_retitle`) and does not churn on repeat sightings.

**2026-09-15 — a capped sitemap traversal says so.** `collect_from_sitemaps` takes
an optional `report` dict and fills `cap` (`url_cap` or `fetch_cap`) and `note`
when `max_per_source` dropped an in-window URL or left sitemaps unread, or when
`max_sitemap_fetches` stopped it with sitemaps still queued. Both caps used to stop
silently while the source reported ok. The return type is unchanged, so `probe.py`
needs nothing; `src/collect.py` stores the note as
`truncated: ...` in `run_source.error` with status still `ok` (crawl_tasks.md T5).

**2026-09-17 — publisher rejection cooldown.** `crawler_html_utils` adds
`REJECTION_STATUSES` (403 plus the existing 429/503 throttle statuses). Discovery
and body fetching use that shared definition; `fetch_html` no longer launches a
browser after a 403. Persistent cooldown state lives in `src/publisher_cooldown.py`.

**2026-09-15 — polite discovery: conditional requests, throttle stop, no guessing
past configured roots.** Measured the same day, one news discovery pass sent 601
requests and 229 MB, nine passes a day; ZEIT had already blocked us for request
volume. Four changes:

- `fetch_sitemap_urls`, `collect_from_sitemaps` and `collect_from_feeds` take an
  optional duck-typed `cache` (`src/polite_http.py` `DiscoveryCache`). Requests carry
  `If-None-Match`/`If-Modified-Since`, and a 304 replays the entries the unchanged
  file yielded before - with sitemap dates already resolved against the index
  `<lastmod>` - so the hints equal a full download's. Plain skipping was rejected: an
  article found by a titled feed and an untitled sitemap would lose its title when
  only the feed went quiet, and `src/bodies.py` `queue_body` requeues a body fetch on
  any changed hint. `collect_from_feeds` now calls `session.get` itself instead of
  `fetch_html(use_playwright_fallback=False)`, which did the same without headers.
- `crawler_html_utils` defines `HostThrottled` and `THROTTLE_STATUSES` (429, 503).
  `fetch_html` re-raises the exception and no longer escalates a 429/503 to
  Playwright; `collect_from_sitemaps` stops its traversal on it. The adapter that
  raises it lives in `src/polite_http.py`.
- `discover_sitemaps` skips its path guesses when `extra_sitemap_urls` is configured.
  faz.net declares no sitemap, so every pass sent 24 HEAD guesses to find the same
  two files, which are now configured.
- `polite_get` accepts `headers`.

Measured after, cold cache then an immediate second pass: 475 and 471 requests,
209 and 180 MB, the same hints per source. The request cut is `feed_urls` and the
dropped guesses. The byte cut is small because the two largest sitemap trees send no
validators - faz.net (97 MB, 104 requests) and dvz.de (29 MB, 90) answer every
conditional request in full; VerkehrsRundschau went 25.6 MB to 0.16 MB, and WELT,
WiWo and Handelsblatt answered most files 304.

**2026-09-13 — `proxy.py` added, then unwired.** A Bright Data ISP-proxy fallback
ported from rewriter was hooked into `crawler_html_utils.fetch_html` and
`scraper_fetch_html.fetch_html`, then removed the same day. Neither hook reached the
body fetch (`src/bodies.py` uses neither function), the collection hook fired on
404s and discarded listing pages, and the laptop's residential IP has no measured
blocking problem. The module stays for VPS disaster recovery; its header says where
to wire it and what to test first.

**2026-09-16 — deleted the code no pipeline path reaches.** Four modules and one
function subtree, about 1,780 lines, removed after tracing every import from
`run.py`, `src/` and `health/`:

- `crawler_google_feeds.py`, `parallel_crawler.py`, `scraper.py`,
  `scraper_fetch_html.py`: nothing outside the deleted set imported any of them.
- `crawler.crawl_site` and its subtree — `ArticleRecord`, `feature_allowed` — plus
  the `crawler_html_utils` functions only it called: `fetch_title_fallback`,
  `sniff_date`, `_fetch_sitemap_dates`, `enrich_dates_light`.

`crawl_site` was the upstream entry point: point it at a URL and a date range and
it discovers everything from scratch. That is the right shape for an ad-hoc crawl
of an unknown site and the wrong one for a fixed source list swept nine times a
day, which is what `src/collect.py` `collect_source` does instead — it calls
`collect_from_sitemaps`, `collect_from_feeds` and `collect_from_frontpage`
directly. Keeping the old entry point around made the vendored crawler look like
a much larger thing to reason about than the ~415 lines of discovery the pipeline
actually runs (crawl_tasks.md T10).

Nothing live changed: 565 tests pass before and after. Stale references in
`proxy.py`, `paywall/*_login.py`, `paywall/handler.py`, `src/probe.py`,
`docs/body_collection.md` and `backup_plan.md` were repointed at the surviving
code. `source_loader.load_blacklist` and `is_url_blacklisted` went with it the same
day, by the owner's decision: `crawl_site` was their only caller,
`input/Blacklist/` never existed in this repository, and per-source exclusion is
`src/collect.py` `url_is_excluded` (`excluded_dirs`, `excluded_url_patterns`,
`excluded_url_substrings`, `excluded_title_substrings`), which is configured per
source rather than as one global URL list.

**2026-09-16 — `fetch_sitemap_urls(strict=True)`, for pinned files.** The
function answers `([], [])` for every failure: a bad status, markup instead of
XML, unparseable XML, a recover parse that salvages nothing. That is right for a
traversal, where one unreadable file among a hundred guesses is normal and the
walk continues, and wrong for a *pinned* file, which is the only place that
source's articles come from - an unreadable one there is indistinguishable from a
quiet day, which is exactly how DVZ and VerkehrsRundschau lost days unnoticed.

`strict` raises instead, adding two checks that only matter when a single file is
load-bearing: the final response URL must still be on the requested host (compared
without `www.`), and the parsed root must be `<urlset>` or `<sitemapindex>`, since
the recover parser will otherwise hand back the first element of whatever a
reorganised URL now serves. An empty but well-formed sitemap raises under neither
setting; the discriminator is whether the file could be read, never whether it was
full. `src/discovery.py` is the only caller, and `collect_from_sitemaps` is
untouched.

**2026-09-16 — the probe reports per file, without a crawler change.**
`collect_from_sitemaps` already offers every fetched file to an optional duck-typed
`cache`, which is the per-file hook the probe needed: `src/probe.py` `FileRecorder`
implements `headers`/`seen`/`remember`, returns no conditional headers (a probe
wants the real file), and keeps each file's entries and validators. `file_yield`
then counts what each file would actually contribute - window, `allowed_dirs`, the
source's exclusion rules, furniture, malformed - and `pin_suggestion` runs a greedy
set cover over those sets to print the `sitemap_urls` block to paste. Nothing in
the vendored crawler changed for any of it.

## googlesearch

| Field | Value |
|---|---|
| Source repository | `https://github.com/jinchiluis/germany_risk_monitor` |
| Source path | `deprecated/google_searcher.py` |
| Source commit | `6a861151c64ae7d36e48561bec72b678370ce067` (`6a86115`) |
| Commit date | 2026-06-13 |
| Copied on | 2026-09-08 |

Google Custom Search JSON API collection: `keywords + site:domain`, returning
records shaped like the crawler's (`site`, `title`, `url`, `published_at`,
`crawled_at`, `summary`), where `summary` carries the search snippet.

It sits in the source repo's `deprecated/` because that project pivoted from
multi-country search collection to single-country sitemap sweeping, not because the
code failed. `deprecated/German-Energy/` holds its output from 2025-11-02 —
28 to 41 articles per run from sites such as `offshore-energy.biz`.

Brandmonitor needs it for the opposite reason: brand mentions are sparse and often
absent from headlines, so sweeping plus title matching has poor recall on the
majors. Full-text search covers what the sweep cannot see.

### Local changes

**2026-09-08 — rewrote the host import.** `from src.crawler.crawler_html_utils`
became `from vendor.newscrawler.crawler_html_utils` (note the source path was
`src.crawler`, an older layout than the news crawler's `src.crawler_news`). Nothing
else changed.

### Known issues, not yet addressed

- `dateRestrict` is hardcoded to `d1` (last 24 hours, relative to now). The
  `--date` / `--start` / `--end` arguments are accepted, threaded through, and then
  ignored — `normalize_items` says "no date filtering - trust dateRestrict". Daily
  runs work; backfill and re-runs do not.
- `date_in_range()` exists but nothing calls it.
- Output goes to timestamped JSONL folders, not SQLite.
