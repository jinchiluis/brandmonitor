#!/usr/bin/env python3
"""
Playwright fallback fetcher for JS-heavy / SPA news sites.
Called only when requests/httpx fails or returns an SPA shell.
"""

from playwright.sync_api import sync_playwright

from src.logger import get_logger
logger = get_logger(__name__)

from src.config import CRAWLER_VERBOSE as _VERBOSE

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Simple in-memory cache to avoid re-fetching same URLs within a run
_HTML_CACHE: dict = {}

def clear_html_cache():
    global _HTML_CACHE
    count = len(_HTML_CACHE)
    _HTML_CACHE = {}
    if _VERBOSE and count > 0:
        logger.info(f"[playwright] Cleared HTML cache ({count} entries)")


def fetch_html_with_playwright(url: str, timeout_ms: int = 40000, use_cache: bool = True) -> bytes:
    """
    Headless Playwright fetch. Use as a fallback only — slower than requests.
    - Waits for DOM ready
    - Detects SPA containers and waits for render
    - Scrolls to trigger lazy loads
    - Caches results in-memory per run
    """
    global _HTML_CACHE

    if use_cache and url in _HTML_CACHE:
        if _VERBOSE: logger.info(f"[playwright] Cache hit {url}")
        return _HTML_CACHE[url]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 850},
                java_script_enabled=True,
                locale="de-DE",
            )
            page = ctx.new_page()

            if _VERBOSE: logger.info(f"[playwright] goto {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as nav_err:
                if _VERBOSE: logger.info(f"[playwright] navigation warning: {nav_err}")
                # Continue — page may be partially loaded but usable

            # Extra wait if this looks like an SPA that needs time to hydrate
            try:
                app_has_content = page.evaluate("""
                    () => {
                        const app = document.querySelector('#app, #root, #__next');
                        return app && app.children.length > 0;
                    }
                """)
                if app_has_content:
                    page.wait_for_timeout(2000)
                    if _VERBOSE: logger.info("[playwright] SPA container detected, waited for render")
            except Exception:
                pass

            # Wait for any recognisable article content
            for sel in ("article", "[class*='article']", "[class*='news']", "main"):
                try:
                    page.wait_for_selector(sel, timeout=5000)
                    if _VERBOSE: logger.info(f"[playwright] content via '{sel}'")
                    break
                except Exception:
                    continue

            # Scroll to trigger lazy-loaded content
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            html = page.content()
            if _VERBOSE: logger.info(f"[playwright] got {len(html)} chars")

            ctx.close()
            browser.close()

            html_bytes = html.encode("utf-8", "ignore")
            if use_cache:
                _HTML_CACHE[url] = html_bytes
            return html_bytes

    except Exception as e:
        logger.info(f"[playwright] failed {url}: {e}")
        return b""
