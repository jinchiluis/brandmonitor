# Vendored NewsCrawler

Article-scraping subset of [jinchiluis/NewsCrawler](https://github.com/jinchiluis/NewsCrawler), used only by the VPS-side `scrape_server.py`. Streamlit Cloud never imports anything from this directory.

## Source
- Repo: `c:/apps/NewsCrawler` (local)
- Commit: `7cbe780ca1302a18752d5e74d5fee7c00f04fb81`
- Copied: 2026-05-20

## Layout
```
newscrawler/src/crawler_news/
  scraper.py                 # entry point: scrape_article(url) -> text
  scraper_fetch_html.py      # robust fetcher: HTTP/2 -> HTTP/1.1 -> Playwright
  crawler_playwright.py      # headless Playwright fallback
  paywall/
    handler.py               # paywall detection + login orchestration
    paywalls.json            # per-domain paywall config (bild/spiegel/welt/zeit/manager-magazin)
    bild_login.py
    spiegel_login.py
    welt_login.py
    zeit_login.py
    manager_magazin_login.py
```

## Patches applied (do not re-sync upstream blindly)
Re-syncing means re-applying these:

1. **Dropped BrightData support** — `crawler_brightdata.py` deleted; brightdata branch and `sources.is_brightdata_enabled(url)` check removed from `scraper_fetch_html.fetch_html()`. German sites don't need it.
2. **Replaced `src.config`** — every `from src.config import CRAWLER_VERBOSE as _VERBOSE` replaced with a local `_VERBOSE = False`. No config.json, no mocker, no LLM agent infra.
3. **Replaced `src.logger`** — every `from src.logger import get_logger; logger = get_logger(__name__)` replaced with `import logging; logger = logging.getLogger(__name__)`. No SMTP, no pipeline log files.

## Files NOT vendored from upstream
Index/discovery crawling (find article URLs from homepages, RSS, Google News) is not needed — users paste URLs directly. Dropped:

- `crawler.py`, `crawler_google_feeds.py`, `crawler_html_utils.py`
- `parallel_crawler.py`, `scraped_source_reader.py`, `source_loader.py`
- `crawler_brightdata.py`
- `src/config.py`, `src/logger.py`, `src/mocker.py`, `config.json`
- `crawler_gov/`, `report/`, `agents/`, `test/`, `toolbox/`, `mock/`, `input/`, `output/`, `log/`

## Re-sync procedure
1. `git -C c:/apps/NewsCrawler log --oneline -5` — pick new commit
2. Copy `src/crawler_news/{scraper.py, scraper_fetch_html.py, crawler_playwright.py, paywall/}` over
3. Re-apply the three patches above
4. Update commit sha in this README

## Customization
Customizations build onto original code, especially handler.py and paywalls.json, because of proxy behaviour. Reference docs:

- `paywall_bandwidth_optimization.md` — paywall Playwright resource blocking / wire-byte accounting.
- `html_scrape_with_proxy.md` — the non-paywall fetch-escalation ladder and the BrightData ISP-session rotation, including the paywall handler's throwaway-peer rotation on `ERR_TUNNEL_CONNECTION_FAILED`.
