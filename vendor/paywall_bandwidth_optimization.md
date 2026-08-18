# Playwright Bandwidth Optimization for Paywall Sites

## Summary of findings

Tested on Axel Springer paywall sites (Bild, Welt) using Playwright + saved login states.
Two separate contexts: **login flow** and **article fetching** — optimizations differ.

---

## Key insight: bild and welt have different paywall architectures

- **Bild** = server-side rendering: article text is in the HTML response when session cookies are valid.
  JS disabled works — `page.content()` returns full article text.
- **Welt** = client-side rendering: server always returns a ~400KB shell (nav, boilerplate, metadata).
  Article text is fetched via JS API calls after session verification. JS disabled returns no article text.
Earlier finding "Blocking ALL JS → OK for Welt" was a false positive.

## Article fetching — bild (JS disabled)

```python
ctx = browser.new_context(
    user_agent=UA,
    viewport={"width": 1366, "height": 850},
    java_script_enabled=False,        # safe for bild — content is server-rendered
    storage_state=str(state_path),
)

BLOCK_TYPES = {"image", "font", "media", "stylesheet"}

def on_route(route):
    if route.request.resource_type in BLOCK_TYPES:
        route.abort()
    else:
        route.continue_()

page.route("**/*", on_route)
page.goto(article_url, wait_until="domcontentloaded")
html = page.content()
```

**Result**: ~82KB wire. Full article text in HTML, extractable via BeautifulSoup.

## Article fetching — welt (JS required)

Welt loads article content via JS API calls after verifying the session with whoami/rosetta.
JS must be enabled. Use a whitelist — minimum 4 domains confirmed by progressive removal:

```python
JS_WHITELIST_WELT_ARTICLE = {
    "www.welt.de",                        # main site JS (content renderer)
    "whoami-web.prod.ps.welt.de",         # session identity — required
    "rosetta.prod.ps.welt.de",            # auth tokens — required
    "rosetta.prod.ps.axelspringer.de",    # auth tokens — required
    # wall-e, wait-web, highlander-web, lefty-next-web — NOT needed for article fetch
}
```

```python
BLOCK_TYPES = {"image", "font", "media", "stylesheet"}

def on_route(route):
    rtype = route.request.resource_type
    dom = route.request.url.split("/")[2] if "/" in route.request.url else ""
    if rtype in BLOCK_TYPES:
        route.abort()
    elif rtype == "script" and dom not in JS_WHITELIST_WELT_ARTICLE:
        route.abort()
    else:
        route.continue_()
```

**Result**: ~320 KB wire.
Scripts are the dominant cost — www.welt.de loads the content renderer bundle.

---

## ⚠️ Stub detection with minimal article JS

**Old stub signals no longer work.**

The stub signals (`"Weiterlesen mit"`, `"Alle Inhalte auf welt.de"`) are rendered
by JS widgets. With Bild JS disabled or Welt JS reduced to the minimal whitelist,
an expired/invalid session can return a page with no text stub signal and no full
article content — the old check gives a false negative.

### Problem
- Valid Bild session + no JS → no text stub signal, article text in HTML ✓
- Blocked Bild session + no JS → no text stub signal, teaser/stub HTML ✗
- Minimal Welt JS can also omit the old rendered text stub signal

### Stub detection

Both `isPaywallShown` and `contentAccessBlocked` are server-rendered inline script tags.
They flip cleanly when present. `contentAccessBlocked` works on both sites;
`isPaywallShown` is Welt-only. Confirmed by testing with/without login state:

| Site | Session | `isPaywallShown` | `contentAccessBlocked` |
|---|---|---|---|
| Bild | no login | NOT FOUND | `true` |
| Bild | valid | NOT FOUND | `false` |
| Welt | no login | `true` | `true` |
| Welt | valid | `false` | `false` |

`isPaywallShown` is Welt-specific (not present on Bild).
`contentAccessBlocked` is present on both — use it as the universal signal.

```python
import re

def has_access(html: str) -> bool:
    """Works for both bild and welt. Returns True if session grants access."""
    m = re.search(r'["\']?contentAccessBlocked["\']?\s*:\s*(true|false)', html)
    return m is not None and m.group(1) == "false"
```

If `contentAccessBlocked: true` persists after a fresh re-login it likely means the ISP exit IP itself was rejected by the login page — the handler rotates to a throwaway `rot<hex>` session and re-logins once more on the fresh exit IP before declaring credentials dead.

**Rejected alternatives:**
- Old stub signals (`"Weiterlesen mit"`) — JS-rendered widget, not present with minimal whitelist
- `articleBody` length in JSON-LD — always ~220 chars on welt regardless of session
- `isAccessibleForFree` in JSON-LD — always `false` for paid articles, doesn't change with session
- DOM text length — unreliable, some stubs are ~1000 chars, same size as short full articles

---

## Login flow optimization

**Rule: keep JS on, keep stylesheets, whitelist JS domains, save state on redirect.**

Stylesheets are required — consent banner and login form need CSS to be visible.
Blocking stylesheets causes a blank/invisible page.

Use a **JS whitelist** not a blacklist — future-proof against new ad/tracking domains.

### Save state on redirect (framenavigated)

The moment the `framenavigated` listener sees the main frame land on
`www.welt.de` / `www.bild.de`, the session cookies/localStorage are already
written. Save state immediately and abort all further requests — the landing
page never needs to load.

```python
from urllib.parse import urlsplit

redirected = False
landing_hosts = {"bild.de", "www.bild.de", "welt.de", "www.welt.de"}

def on_navigated(frame):
    nonlocal redirected
    if frame == page.main_frame and not redirected:
        if urlsplit(frame.url).hostname in landing_hosts:
            redirected = True
            ctx.storage_state(path=str(state_path))

def on_route(route):
    if redirected:
        route.abort()
        return
    # ... whitelist logic below

page.on("framenavigated", on_navigated)
```

**Critical**: use exact hostname match (`urlsplit(url).hostname not in landing_hosts`)
NOT substring (`"welt.de" in url`) — the login page is at `signin.auth.welt.de`
which contains `"welt.de"` and would trigger early if using substring match.

**welt_login.py fix**: wrap the extra `page.goto("/meinewelt/einstellungen")` in
try/except — it throws ERR_FAILED when aborted by the redirect flag, but the state
is already saved so this is fine:
```python
try:
    page.goto("https://www.welt.de/meinewelt/einstellungen", wait_until="domcontentloaded", timeout=60000)
except PlaywrightError as e:
    if "ERR_FAILED" not in str(e):
        raise
```

### JS whitelist per site (login)

```python
JS_WHITELIST_LOGIN = {
    "bild_login": {
        "signin.auth.bild.de",
        "rosetta.prod.ps.axelspringer.de",  # required
        "wait-web.prod.ps.bild.de",         # required
        # wall-e, whoami-web, highlander-web, lefty-next-web — NOT needed
    },
    "welt_login": {
        "signin.auth.welt.de",
        "rosetta.prod.ps.axelspringer.de",  # required — spinner without it
        "wait-web.prod.ps.welt.de",         # required — spinner without it
        # wall-e, whoami-web, highlander-web, lefty-next-web — NOT needed
    },
}

BLOCK_TYPES_LOGIN = {"image", "font", "media"}
# Do NOT add "stylesheet" — breaks consent banner rendering
```

**Result**: both logins confirmed working. State saved on redirect (not after full page load).

---

## How to identify blockable JS domains for a new site

```python
scripts = []
page.on("response", lambda r: scripts.append((len(r.body()), r.url))
        if r.request.resource_type == "script" else None)
page.goto(url, wait_until="domcontentloaded")
page.wait_for_timeout(3000)

scripts.sort(reverse=True)
for size, url in scripts:
    print(f"{size/1024:7.1f}K  {url.split('/')[2]}")
```

**Safe to block** — look for:
- Consent/CMP platforms: `cmp*.`, `consent.`, `cdn.privacy-mgmt.com`, `gdpr-*`
- Tag managers: `tiqcdn.com`, `googletagmanager.com`, `utag*`
- Ad networks: `*cdn.com` with ad-sounding names, `jnt.*`, `asadcdn`
- Chatbots: `moin.ai`, `intercom`, `drift`
- Tracking: `simetra.*`, `ringieraxelspringer.tech`

**Treat as candidates to verify before blocking during login**:
- `signin.auth.*` or `login.*` — the login form
- `rosetta*`, `wait-web*`, `whoami*`, `identity*` — session/auth related
- `wall-e.*`, `paywall.*` — paywall related

**For article fetching**: check individually

---

## Bandwidth summary

Wire bytes measured via CDP `encodedDataLength`.

| Context | Before | After | Method |
|---|---|---|---|
| Bild article | ~5MB | ~40 KB | JS disabled + asset blocking (server-rendered) |
| Welt article | ~2.4MB | ~320 KB | JS whitelist (4 domains) + asset blocking |
| Bild login | ~3.3MB | ~313 KB | JS whitelist (3 domains) + redirect-save |
| Welt login | ~3.3MB | ~341 KB | JS whitelist (3 domains) + redirect-save |

Note: login transfer still includes multiple OAuth page navigations. In the current
VPS measurement the document portion was ~126 KB for Bild and ~159 KB for Welt;
CSS/document are still needed for the login flow.

> All wire byte figures measured via CDP `Network.loadingFinished` → `encodedDataLength`.
> `resp.body()` in Playwright returns decompressed size (~2x larger than wire bytes).
> Browser DevTools "transferred" column matches CDP encodedDataLength.
> BrightData usage counters can be higher because CDP sums finished browser response
> bytes only; proxy accounting can also include request bytes, warmups, and proxy overhead.
