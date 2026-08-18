"""
Paywall handler — called from scraper_fetch_html.fetch_html().
Files was modified to support BD proxy and re-login on stub detection

Flow:
  1. get_paywall_cfg(url)  → returns config dict or None
  2. fetch_paywall_article(url, cfg) → logs in if needed, detects stub, returns HTML str
"""
import importlib.util
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import logging
import tldextract

from .. import bandwidth

logger = logging.getLogger(__name__)
_VERBOSE = False

_PAYWALL_DIR = Path(__file__).parent
_PAYWALLS_CACHE = None
_REFRESHED_THIS_RUN = set()  # domains logged-in at least once this process run
_DEAD_DOMAINS = set()        # domains where re-login failed (expired subscription etc.)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_AXEL_SPRINGER_LOGIN_MODULES = frozenset({"bild_login", "welt_login"})
_AXEL_SPRINGER_BLOCKED_RE = re.compile(
    r"(?<![A-Za-z0-9_$])['\"]?contentAccessBlocked['\"]?\s*:\s*true\b"
)
_AXEL_SPRINGER_LOGIN_BLOCKED_TYPES = frozenset({"image", "font", "media"})
_AXEL_SPRINGER_ARTICLE_BLOCKED_TYPES = frozenset({"image", "font", "media", "stylesheet"})
_AXEL_SPRINGER_LOGIN_LANDING_HOSTS = {
    "bild_login": frozenset({"bild.de", "www.bild.de"}),
    "welt_login": frozenset({"welt.de", "www.welt.de"}),
}
_AXEL_SPRINGER_LOGIN_SCRIPT_WHITELISTS = {
    "bild_login": frozenset({
        "signin.auth.bild.de",
        "rosetta.prod.ps.axelspringer.de",
        "wait-web.prod.ps.bild.de",
    }),
    "welt_login": frozenset({
        "signin.auth.welt.de",
        "rosetta.prod.ps.axelspringer.de",
        "wait-web.prod.ps.welt.de",
    }),
}
_WELT_ARTICLE_SCRIPT_WHITELIST = frozenset({
    "www.welt.de",
    "whoami-web.prod.ps.welt.de",
    "rosetta.prod.ps.welt.de",
    "rosetta.prod.ps.axelspringer.de",
})


def _request_domain(url):
    return (urlsplit(url).hostname or "").lower()


def _apply_request_blocking(page, blocked_types, script_whitelist=None, abort_all_after=None):
    def route_request(route):
        request = route.request
        request_type = request.resource_type
        request_domain = _request_domain(request.url)
        if abort_all_after is not None and abort_all_after():
            route.abort()
            return
        if request_type in blocked_types:
            route.abort()
            return
        if script_whitelist is not None and request_type == "script" and request_domain not in script_whitelist:
            route.abort()
            return
        route.continue_()

    page.route("**/*", route_request)


def _apply_login_request_blocking(page, login_module, redirect_state=None):
    if login_module in _AXEL_SPRINGER_LOGIN_MODULES:
        _apply_request_blocking(
            page,
            _AXEL_SPRINGER_LOGIN_BLOCKED_TYPES,
            _AXEL_SPRINGER_LOGIN_SCRIPT_WHITELISTS[login_module],
            (lambda: redirect_state["redirected"]) if redirect_state else None,
        )


def _watch_login_landing(page, ctx, state_path, login_module):
    landing_hosts = _AXEL_SPRINGER_LOGIN_LANDING_HOSTS.get(login_module)
    if not landing_hosts:
        return None

    state = {"redirected": False, "saved": False}

    def save_state_on_landing(frame):
        if state["redirected"] or frame != page.main_frame:
            return
        if _request_domain(frame.url) not in landing_hosts:
            return

        state["redirected"] = True
        try:
            ctx.storage_state(path=str(state_path))
            state["saved"] = True
            logger.info("[paywall] Login landing reached - state saved to %s", state_path)
        except Exception as e:
            logger.info("[paywall] Early login state save failed: %s", e)

    page.on("framenavigated", save_state_on_landing)
    return state


def _apply_article_request_blocking(page, login_module):
    if login_module == "bild_login":
        _apply_request_blocking(page, _AXEL_SPRINGER_ARTICLE_BLOCKED_TYPES)
    elif login_module == "welt_login":
        _apply_request_blocking(
            page,
            _AXEL_SPRINGER_ARTICLE_BLOCKED_TYPES,
            _WELT_ARTICLE_SCRIPT_WHITELIST,
        )


def _article_context_kwargs(login_module):
    if login_module == "bild_login":
        return {"java_script_enabled": False}
    return {}


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



def _fetch_once(p, url, state_path, consent_selectors, proxy_cfg, proxy_name, session, login_module):
    """One Playwright fetch attempt with a specific proxy session. Returns HTML on
    success; lets goto/transport exceptions propagate so the caller can decide
    whether to rotate the exit peer."""
    launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg
        logger.info("[paywall] Using %s proxy for article fetch (session %s)", proxy_name or "configured", session or "-")

    browser = p.chromium.launch(**launch_kwargs)
    try:
        ctx_kwargs = {
            "user_agent": UA,
            "viewport": {"width": 1366, "height": 850},
            **_article_context_kwargs(login_module),
        }
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)

        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        bandwidth.attach(page)
        if proxy_name:
            bandwidth.set_proxy(proxy_name, session)
        _apply_article_request_blocking(page, login_module)
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
            return html
        finally:
            ctx.close()
    finally:
        browser.close()


def _playwright_fetch(
    url,
    state_path,
    consent_selectors=None,
    proxy_cfg=None,
    proxy_name=None,
    login_module=None,
):
    """Fetch article HTML via Playwright using a saved login state.
    Saves updated state after each fetch so refreshed session cookies are persisted.

    ERR_TUNNEL_CONNECTION_FAILED is a Chrome-level tunnel error — it is unrelated
    to BrightData session IDs and rotating to a throwaway session does not help.
    We retry the same sticky session up to TUNNEL_MAX_RETRIES times with a short
    wait, which covers transient Chrome tunnel failures that resolve on their own
    within seconds. Warmup failure means BrightData found no peer after its own
    internal retries — we give up immediately."""
    from playwright.sync_api import sync_playwright

    if proxy_cfg and not _warm_proxy(proxy_cfg, _PROXY_WARMUP_URL):
        logger.info("[paywall] warmup found no peer — giving up")
        bandwidth.note_error("warmup: no peer")
        return ""

    session = _session_of(proxy_cfg)
    with sync_playwright() as p:
        for attempt in range(TUNNEL_MAX_RETRIES + 1):
            try:
                return _fetch_once(
                    p, url, state_path, consent_selectors,
                    proxy_cfg, proxy_name, session, login_module,
                )
            except Exception as e:
                if proxy_cfg and _is_tunnel_error(e) and attempt < TUNNEL_MAX_RETRIES:
                    logger.info(
                        "[paywall] tunnel error on session %s (attempt %d/%d) — waiting %ds before retry",
                        session or "-", attempt + 1, TUNNEL_MAX_RETRIES + 1, TUNNEL_RETRY_WAIT_S,
                    )
                    time.sleep(TUNNEL_RETRY_WAIT_S)
                    continue
                logger.info("[paywall] fetch error: %s", e)
                bandwidth.note_error(_error_cause(e))
                return ""


_BRD_HOST = "brd.superproxy.io"
_BRD_PORT = 33335
_BRD_CUSTOMER = "brd-customer-hl_8bdecc95"
_PROXY_WARMUP_URL = "https://www.gstatic.com/generate_204"
_BRD_ZONES = {
    "isp": "isp_proxy1",
    "residential": "residential_proxy3",
}

def _proxy_cfg(zone: str, session: str | None = None) -> dict | None:
    """Return a Playwright proxy dict for the given zone, or None if creds missing."""
    zone_name = _BRD_ZONES.get(zone)
    if not zone_name:
        return None
    password = os.environ.get("BRD_PASS_ISP" if zone == "isp" else f"BRD_PASS_{zone.upper()}", "")
    if not password:
        logger.info("[paywall] BrightData %s proxy requested but BRD_PASS_%s not set", zone, zone.upper())
        return None
    username = f"{_BRD_CUSTOMER}-zone-{zone_name}"
    if session:
        safe_session = "".join(ch for ch in session if ch.isalnum() or ch in "_-")
        if safe_session:
            username = f"{username}-session-{safe_session}"
    return {
        "server": f"http://{_BRD_HOST}:{_BRD_PORT}",
        "username": username,
        "password": password,
    }


def _proxy_url(proxy_cfg: dict) -> str:
    """Convert a Playwright proxy dict to a requests proxy URL."""
    server = proxy_cfg["server"]
    parsed = urlsplit(server)
    scheme = parsed.scheme or "http"
    host = parsed.netloc or parsed.path
    username = quote(proxy_cfg.get("username", ""), safe="")
    password = quote(proxy_cfg.get("password", ""), safe="")
    auth = f"{username}:{password}@" if username or password else ""
    return f"{scheme}://{auth}{host}"


def _session_of(proxy_cfg: dict | None) -> str | None:
    """Extract the sticky session id embedded in a proxy_cfg username, if any."""
    if not proxy_cfg:
        return None
    username = proxy_cfg.get("username", "")
    return username.split("-session-", 1)[1] if "-session-" in username else None



def _new_throwaway_session() -> str:
    """A random, single-use session id. The `rot` prefix makes a forced rotation
    obvious in the monitor's proxy_session column (vs. the sticky `bild`/`welt`)."""
    return "rot" + secrets.token_hex(4)


# ERR_TUNNEL_CONNECTION_FAILED is a Chrome-level tunnel error, not a BrightData
# session problem — rotating to a new session ID does not help. We retry the same
# sticky session with a short wait; transient Chrome tunnel issues typically resolve
# within seconds.
TUNNEL_MAX_RETRIES = 3   # attempts after the first = 4 total, 24s max wait
TUNNEL_RETRY_WAIT_S = 8


# Chrome-level tunnel error. ERR_PROXY_CONNECTION_FAILED is excluded on purpose
# (same superproxy host — a retry won't help there).
_TUNNEL_ERROR = "ERR_TUNNEL_CONNECTION_FAILED"


def _is_tunnel_error(exc: Exception) -> bool:
    return _TUNNEL_ERROR in str(exc)


def _error_cause(exc: Exception) -> str:
    """A concise cause string for the run log — the `net::ERR_…` code if present,
    else a short type/message form."""
    m = re.search(r"net::ERR_[A-Z_]+", str(exc))
    return m.group(0) if m else f"{type(exc).__name__}: {exc}".strip()[:200]


def _warm_proxy(proxy_cfg: dict | None, url: str, attempts: int = 3) -> bool:
    """Pre-open the BrightData tunnel so the session binds an exit IP.

    Retries the *same* session (BrightData often returns `400 Peer not found`
    until a peer is allocated). Returns True once a peer is bound, False if no
    peer could be bound after `attempts`.
    """
    if not proxy_cfg:
        return False

    import requests

    proxy_url = _proxy_url(proxy_cfg)
    proxies = {"http": proxy_url, "https": proxy_url}
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    last_err = None
    for attempt in range(attempts):
        try:
            with requests.get(url, headers=headers, proxies=proxies, timeout=(3, 8), stream=True):
                return True
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(0.2)
    logger.info("[paywall] proxy warmup failed for %s: %s", url, last_err)
    return False


def _relogin(cfg, state_path, proxy_session_override=None):
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

    # Some sites (Axel Springer / myPass) block datacenter IPs — route login through ISP proxy
    proxy_name = cfg.get("proxy")
    session = proxy_session_override or cfg.get("proxy_session")
    proxy_cfg = _proxy_cfg(proxy_name, session) if proxy_name else None
    launch_kwargs = {
        "headless": False,  # headful — Cloudflare Turnstile blocks headless login
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if proxy_cfg:
        _warm_proxy(proxy_cfg, _PROXY_WARMUP_URL)
        launch_kwargs["proxy"] = proxy_cfg
        logger.info("[paywall] Using %s proxy for %s login", proxy_name, cfg["login_module"])

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        bandwidth.attach(page)
        if proxy_name:
            bandwidth.set_proxy(proxy_name)
        redirect_state = _watch_login_landing(page, ctx, state_path, cfg["login_module"])
        _apply_login_request_blocking(page, cfg["login_module"], redirect_state)
        try:
            login_mod.login(page, email, password)
            if not redirect_state or not redirect_state["saved"]:
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


def _paywall_trigger(html: str, stub_signals, login_module: str):
    if not html:
        return None

    if login_module in _AXEL_SPRINGER_LOGIN_MODULES and _AXEL_SPRINGER_BLOCKED_RE.search(html):
        return "contentAccessBlocked: true"

    return next((signal for signal in stub_signals if signal in html), None)


def fetch_paywall_article(url, cfg):
    """Full paywall fetch: use saved state when available, re-login if missing or expired."""
    base_url = url  # original URL before any suffix is appended
    if "url_append" in cfg:
        suffix = cfg["url_append"]
        if not url.rstrip("/").endswith(suffix):
            url = url.rstrip("/") + suffix

    state_path = _PAYWALL_DIR / "states" / cfg["state_file"]
    consent_selectors = cfg.get("consent_selectors", [])
    stub_signals = cfg.get("stub_signals", [])
    domain = cfg["login_module"]
    proxy_name = cfg.get("proxy")
    proxy_cfg = _proxy_cfg(proxy_name, cfg.get("proxy_session")) if proxy_name else None

    if domain in _DEAD_DOMAINS:
        raise RuntimeError(f"[paywall] Skipping {domain} — credentials expired or invalid (failed earlier this run)")

    if domain not in _REFRESHED_THIS_RUN and not state_path.exists():
        if _VERBOSE: logger.info("[paywall] First access this run — refreshing session for %s...", domain)
        _relogin(cfg, state_path)
        _REFRESHED_THIS_RUN.add(domain)

    html = _playwright_fetch(
        url,
        state_path,
        consent_selectors,
        proxy_cfg,
        proxy_name,
        domain,
    )

    # If url_append produced a 404 page, fall back to the base URL (e.g. zeit.de's
    # single page article URLs don't have a /komplettansicht variant).
    if html and "url_append" in cfg and url != base_url and _is_not_found(html):
        logger.info("[paywall] url_append path returned 404 (%s), retrying base URL %s", url, base_url)
        html = _playwright_fetch(
            base_url,
            state_path,
            consent_selectors,
            proxy_cfg,
            proxy_name,
            domain,
        )

    triggered = _paywall_trigger(html, stub_signals, domain)
    if triggered:
        logger.info("[paywall] Paywall trigger detected (%r) — re-logging in...", triggered)
        _relogin(cfg, state_path)
        html = _playwright_fetch(
            url,
            state_path,
            consent_selectors,
            proxy_cfg,
            proxy_name,
            domain,
        )
        # Still a stub after re-login — login page may have rejected the exit IP;
        # rotate to a throwaway session and retry the full login+fetch once.
        still_triggered = _paywall_trigger(html, stub_signals, domain)
        if still_triggered and proxy_name:
            rot_session = _new_throwaway_session()
            rot_cfg = _proxy_cfg(proxy_name, rot_session)
            logger.info("[paywall] still blocked after re-login — rotating exit IP to %s", rot_session)
            _relogin(cfg, state_path, proxy_session_override=rot_session)
            html = _playwright_fetch(url, state_path, consent_selectors, rot_cfg, proxy_name, domain)
            still_triggered = _paywall_trigger(html, stub_signals, domain)
        if still_triggered:
            _DEAD_DOMAINS.add(domain)
            raise RuntimeError(f"[paywall] {domain} paywall trigger persists after re-login, skipping all remaining articles")

    return html
