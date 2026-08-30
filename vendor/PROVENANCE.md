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

`crawler_brightdata.py` is included because `scraper_fetch_html.py` imports it and it
may support social-platform collection. It is not the default path for website
fetching on the laptop.

### Excluded

- `scraped_source_reader.py`: unused by the copied modules
- `states/`: runtime cookies, watermarks, and downloaded data
- `__pycache__/`: generated files

### Integration requirements

The copied modules still reference their original host application:

```text
src.config
src.logger
src.crawler_news.*
```

Brandmonitor will provide `src/config.py` and `src/logger.py`. The
`src.crawler_news.*` imports should be rewritten to the vendored package path during
initial integration.

### Local changes

None yet. Add short entries here with the affected files and reason when material
changes are made.
