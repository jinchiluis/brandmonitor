# Vendor Provenance

Everything under `vendor/` was copied verbatim from another repo. This file records
what came from where, so divergence stays visible.

## newscrawler

| | |
|---|---|
| Source repo | `germany_risk_monitor` — `https://github.com/jinchiluis/germany_risk_monitor` |
| Source path | `src/crawler_news/` |
| Commit | `6a861151c64ae7d36e48561bec72b678370ce067` (`6a86115`, `origin/master`) |
| Commit date | 2026-06-13 |
| Copied on | 2026-08-18 |
| Modified since copy | no |

Copied 1:1, no edits:

```
crawler.py                 crawler_brightdata.py
crawler_google_feeds.py    crawler_html_utils.py
crawler_playwright.py      parallel_crawler.py
scraper.py                 scraper_fetch_html.py
source_loader.py
paywall/handler.py         paywall/paywalls.json
paywall/{bild,manager_magazin,spiegel,welt,zeit}_login.py
```

Taken from `origin/master`, **not** the local `c:/apps/germany_risk_monitor` working
copy, which was 2 commits behind at the time. One of those commits patched
`crawler.py` (logging + error handling). Extracted with `git archive origin/master`,
so the local GRM checkout was left untouched — it may still be behind.

**Not copied:**
- `scraped_source_reader.py` — nothing in the copied set imports it
- `states/` — login session cookies; regenerate by logging in
- `__pycache__/`

`crawler_brightdata.py` is not in the plan's §4 take list but is included anyway:
`scraper_fetch_html.py:28` imports it at module level, so the tree does not import
without it. It also backs the `provider:brightdata` connector in §5.1.

## Host integration requirements

The vendored code is not self-contained. It imports from the surrounding app:

```
from src.config import ...
from src.logger import ...
from src.crawler_news.crawler import ...
from src.crawler_news.source_loader import ...
```

Two consequences when `src/` gets built:

1. **`src.logger` vs. our planned `src/log.py`.** The names must match or the vendor
   imports need patching. Decide once, deliberately — patching the vendor means this
   file's "modified since copy" line changes to yes.
2. **`src.crawler_news.*` absolute imports** assume the GRM package layout, not
   `vendor/newscrawler/src/crawler_news/`. Either mirror the path, add a shim, or
   rewrite the imports on first integration.

## Pending ports from rewriter

rewriter (`c:/apps/rewriter`) is otherwise **not** a source repo. Its fetch fork leads
GRM only in BrightData proxy machinery — ISP session pinning, tunnel-error rotation,
bandwidth accounting — which existed to compensate for a datacenter IP. D8 removes the
need. Verified: 4 of 5 login modules are byte-identical, and the only `paywalls.json`
difference is the `proxy`/`proxy_session` entries for bild and welt.

Two small things there are still worth taking, neither proxy-related:

1. **`contentAccessBlocked` regex** in rewriter's `paywall/handler.py` `_paywall_trigger`
   (~5 lines). Reads the JS payload instead of only string-matching `stub_signals`.
   Welt needs it. Called out in plan §4.
2. **Welt `ERR_FAILED` guard** — rewriter's `welt_login.py` wraps the settings-page
   `page.goto` in a `try/except PlaywrightError` that swallows `ERR_FAILED`. Not in
   the plan; found by diffing. GRM's version lets that navigation raise.

Neither is applied yet — the tree is a clean 1:1 copy.

## Notes

This is a pruned and heavily patched fork of NewsCrawler. Treat it as our code — do
not restructure it to track upstream. GRM is the origin of record for the initial copy
only; after the first local edit the two diverge for good, and the "modified since
copy" line above should say so.

The BrightData paths stay available but unused by default for site fetching: on a
residential IP (D8) there is nothing to rotate to, and the proxy is the fallback only
if the home IP gets blocked. Socials still use it where applicable.
