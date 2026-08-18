"""
Paywall handler — called from scraper_fetch_html.fetch_html().

Flow:
  1. get_paywall_cfg(url)  → returns config dict or None
  2. fetch_paywall_article(url, cfg) → logs in if needed, detects stub, returns HTML str
"""
import importlib.util
import json
import os
from pathlib import Path

import tldextract
from src.logger import get_logger

logger = get_logger(__name__)
from src.config import CRAWLER_VERBOSE as _VERBOSE

_PAYWALL_DIR = Path(__file__).parent
_PAYWALLS_CACHE = None
_REFRESHED_THIS_RUN = set()  # domains logged-in at least once this process run
_DEAD_DOMAINS = set()        # domains where re-login failed (expired subscription etc.)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _load_paywalls():
    global _PAYWALLS_CACHE
    if _PAYWALLS_CACHE is None:
        try:
            with open(_PAYWALL_DIR / "paywalls.json", "r", encoding="utf-8") as f:
                _PAYWALLS_CACHE = json.load(f)
        except Exception:
            _PAYWALLS_CACHE = {}
    return _PAYWALLS_CACHE


def get_paywall_cfg(url):
    """Return paywall config for this URL's domain, or None."""
    ext = tldextract.extract(url)
    domain = f"{ext.domain}.{ext.suffix}"
    return _load_paywalls().get(domain)


def _playwright_fetch(url, state_path, consent_selectors=None):
    """Fetch article HTML via Playwright using a saved login state.
    Saves updated state after each fetch so refreshed session cookies are persisted."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx_kwargs = {"user_agent": UA, "viewport": {"width": 1366, "height": 850}}
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)

        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for sel in (consent_selectors or []):
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(1500)
            html = page.content()
            # Persist any refreshed session cookies so the state file stays fresh
            try:
                ctx.storage_state(path=str(state_path))
            except Exception:
                pass
        except Exception as e:
            logger.info("[paywall] fetch error: %s", e)
            html = ""
        finally:
            ctx.close()
            browser.close()
    return html


def _relogin(cfg, state_path):
    """Run the site-specific login flow and save a fresh storage_state."""
    from playwright.sync_api import sync_playwright

    email = os.environ.get(cfg["email_env"], "")
    password = os.environ.get(cfg["password_env"], "")
    if not email or not password:
        logger.info("[paywall] Missing credentials — set %s / %s in .env", cfg['email_env'], cfg['password_env'])
        return

    login_path = _PAYWALL_DIR / f"{cfg['login_module']}.py"
    spec = importlib.util.spec_from_file_location(cfg["login_module"], login_path)
    login_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(login_mod)

    state_path.parent.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # headful — Cloudflare Turnstile blocks headless login
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        try:
            login_mod.login(page, email, password)
            ctx.storage_state(path=str(state_path))
            logger.info("[paywall] Login OK — state saved to %s", state_path)
        except Exception as e:
            logger.info("[paywall] Login failed: %s", e)
        finally:
            ctx.close()
            browser.close()


_NOT_FOUND_SIGNALS = [
    "Dokument nicht gefunden",
    "404 Not Found",
    "Page not found",
    "Seite nicht gefunden",
]


def _is_not_found(html: str) -> bool:
    return any(s in html for s in _NOT_FOUND_SIGNALS)


def fetch_paywall_article(url, cfg):
    """Full paywall fetch: always login fresh on first access per run, detect stub, re-login if expired."""
    base_url = url  # original URL before any suffix is appended
    if "url_append" in cfg:
        suffix = cfg["url_append"]
        if not url.rstrip("/").endswith(suffix):
            url = url.rstrip("/") + suffix

    state_path = _PAYWALL_DIR / "states" / cfg["state_file"]
    consent_selectors = cfg.get("consent_selectors", [])
    stub_signals = cfg.get("stub_signals", [])
    domain = cfg["login_module"]

    if domain in _DEAD_DOMAINS:
        raise RuntimeError(f"[paywall] Skipping {domain} — credentials expired or invalid (failed earlier this run)")

    if domain not in _REFRESHED_THIS_RUN:
        if _VERBOSE: logger.info("[paywall] First access this run — refreshing session for %s...", domain)
        _relogin(cfg, state_path)
        _REFRESHED_THIS_RUN.add(domain)

    html = _playwright_fetch(url, state_path, consent_selectors)

    # If url_append produced a 404 page, fall back to the base URL (e.g. zeit.de's
    # single page article URLs don't have a /komplettansicht variant).
    if html and "url_append" in cfg and url != base_url and _is_not_found(html):
        logger.info("[paywall] url_append path returned 404 (%s), retrying base URL %s", url, base_url)
        html = _playwright_fetch(base_url, state_path, consent_selectors)

    triggered = next((s for s in stub_signals if s in html), None) if stub_signals and html else None
    if triggered:
        logger.info("[paywall] Stub detected (%r) — re-logging in...", triggered)
        _relogin(cfg, state_path)
        html = _playwright_fetch(url, state_path, consent_selectors)
        # Still a stub after re-login → credentials are dead
        still_stub = next((s for s in stub_signals if s in html), None) if stub_signals and html else None
        if still_stub:
            _DEAD_DOMAINS.add(domain)
            raise RuntimeError(f"[paywall] {domain} credentials expired — stub persists after re-login, skipping all remaining articles")

    return html
