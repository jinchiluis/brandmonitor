# Vendor Provenance

Everything under `vendor/` was copied verbatim from another repo. This file records
what came from where, so divergence is visible later.

## newscrawler

| | |
|---|---|
| Source repo | `rewriter` — `git@github.com:jinchiluis/rewriter.git` |
| Source path | `vendor/newscrawler/src/crawler_news/` |
| Commit | `6f498893df0d28e113fa662b03af14a690720ac1` (`6f49889`) |
| Commit date | 2026-06-04 |
| Copied on | 2026-08-18 |
| Modified since copy | no |

Copied 1:1, no edits. Files:

```
bandwidth.py
crawler_playwright.py
scraper.py
scraper_fetch_html.py
paywall/handler.py
paywall/paywalls.json
paywall/{bild,manager_magazin,spiegel,welt,zeit}_login.py
```

Reference docs copied alongside: `README.md`, `html_scrape_with_proxy.md`,
`paywall_bandwidth_optimization.md`.

**Not copied:** `paywall/states/` (login session cookies — gitignored, regenerate by
logging in) and `__pycache__/`.

## Notes

This is already a pruned and heavily patched fork of NewsCrawler. It is **our code** —
do not restructure it to track upstream. Treat rewriter as the origin of record only
for the initial copy; after the first local edit the two diverge for good, and this
file's "modified since copy" line should say so.

### Known gaps vs. the germany_risk_monitor fork

rewriter's fork is the newer and more capable one for fetching, but GRM's
`scraper_fetch_html.py` has four functions this copy lacks:

```
_amp_variants  should_try_amp  _attempt_httpx  _attempt_requests
```

rewriter dropped AMP fallback because it targets five known German majors. This
product hits ~50 heterogeneous small/niche sites where AMP variants are common,
cheap, and often more lightly walled. Porting AMP fallback forward is an open item —
as a separate module, not merged function-by-function into `scraper_fetch_html.py`.

Discovery (sitemap BFS, Google feeds, parallel crawl, source loading) does not exist
in this fork at all and comes from germany_risk_monitor instead.

### Proxy stack status

The escalation ladder, pinned ISP session, and `ERR_TUNNEL_CONNECTION_FAILED`
rotation exist to compensate for a datacenter IP. This pipeline runs from a
residential IP, so that machinery stays **available but off by default** — it is the
fallback if a site blocks the home IP, where no rotation is possible.
