#!/usr/bin/env python3
"""
Playwright fallback fetcher for JS-heavy / SPA news sites.
Called only when requests/httpx fails or returns an SPA shell.
"""

import logging
from playwright.sync_api import sync_playwright

from . import bandwidth

logger = logging.getLogger(__name__)
_VERBOSE = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Block these resource types on proxied fetches only — they burn billed ISP
# bandwidth and are not needed for text extraction. JS/XHR are kept so SPA
# sites still render. Naked (unproxied) fetches load the whole page.
_PROXY_BLOCKED_TYPES = {"image", "font", "media", "stylesheet"}

# Simple in-memory cache to avoid re-fetching same URLs within a run
_HTML_CACHE: dict = {}

def clear_html_cache():
    global _HTML_CACHE
    count = len(_HTML_CACHE)
    _HTML_CACHE = {}
    if _VERBOSE and count > 0:
        logger.info(f"[playwright] Cleared HTML cache ({count} entries)")


def fetch_html_with_playwright(
    url: str,
    timeout_ms: int = 40000,
    use_cache: bool = True,
    proxy_cfg: dict | None = None,
    proxy_name: str | None = None,
) -> bytes:
    """
    Headless Playwright fetch. Use as a fallback only — slower than requests.
    - Waits for DOM ready
    - Detects SPA containers and waits for render
    - Scrolls to trigger lazy loads
    - Caches results in-memory per run

    When ``proxy_cfg`` is given, routes Chromium through that BrightData proxy
    and blocks image/css/font/media to keep billed bandwidth down; the warm-up
    and bandwidth tagging mirror the paywall handler.
    """
    global _HTML_CACHE

    # Proxied results must not collide with naked ones for the same URL.
    cache_key = (url, proxy_name)
    if use_cache and cache_key in _HTML_CACHE:
        if _VERBOSE: logger.info(f"[playwright] Cache hit {url}")
        return _HTML_CACHE[cache_key]

    if proxy_cfg:
        # Pre-open the BrightData tunnel before Chromium launches (same as paywall handler).
        from .paywall.handler import _warm_proxy, _PROXY_WARMUP_URL
        _warm_proxy(proxy_cfg, _PROXY_WARMUP_URL)

    try:
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
                logger.info("[playwright] Using %s proxy for fetch", proxy_name or "configured")
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 850},
                java_script_enabled=True,
                locale="de-DE",
            )
            page = ctx.new_page()
            bandwidth.attach(page)
            if proxy_name:
                bandwidth.set_proxy(proxy_name, _proxy_session(proxy_cfg))

            if proxy_cfg:
                def _route(route):
                    if route.request.resource_type in _PROXY_BLOCKED_TYPES:
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", _route)

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
                _HTML_CACHE[cache_key] = html_bytes
            return html_bytes

    except Exception as e:
        logger.info(f"[playwright] failed {url}: {e}")
        return b""


def _proxy_session(proxy_cfg: dict | None) -> str | None:
    """Extract the sticky session token from a BrightData username, if present."""
    if not proxy_cfg:
        return None
    username = proxy_cfg.get("username", "")
    marker = "-session-"
    idx = username.find(marker)
    return username[idx + len(marker):] if idx != -1 else None
