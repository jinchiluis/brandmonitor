# HTML Scrape with ISP Proxy — Fetch Escalation Ladder

Reference for how `vendor/newscrawler/.../scraper_fetch_html.py:fetch_html()` fetches a
non-paywall article, and why it routes through a BrightData ISP proxy. Companion to
`vendor/paywall_bandwidth_optimization.md` (which covers the paywall Playwright
resource blocking) and the paywall flow in `paywall/handler.py`.

## The problem

The VPS scrape server runs on a Contabo **datacenter IP**. Many publishers sit behind
Cloudflare, which blocks datacenter IPs outright:

- static `httpx`/`requests` fetches get **HTTP 403** or a raw **TCP connection reset**;
- a headless Playwright render on the same datacenter IP only loads Cloudflare's
  *"Sicherheitsüberprüfung wird durchgeführt" / "Performing security verification"*
  interstitial (~30 KB, extracts to a few hundred chars of junk).

The old fetcher fanned out ~24 static attempts (httpx/requests × desktop/android ×
referer × AMP variants) and then "trusted any non-empty Playwright content" — so on a
blocked site it spent ~85 s stacking timeouts and then returned the **challenge page**
as if it were the article (e.g. 782 chars of "security verification" text).

A residential/ISP exit IP is not blocked: the same URL returns HTTP 200 with the full
article. So the fix is to escalate from a free datacenter fetch to a cheap ISP-proxy
fetch, and only pay for a browser render as a last resort.

## The escalation ladder

`fetch_html()` runs cheap-to-expensive tiers and stops at the first that returns a real
article. Paywall sites (`get_paywall_cfg`) short-circuit to their own authenticated
Playwright path before any of this.

```
Tier 1  naked httpx (datacenter IP), HTML-only
          200 + real content   -> done
          200 + JS/SPA shell    -> Tier 3 (naked Playwright)
          403 / reset / timeout -> Tier 2 (ISP proxy)
Tier 2  ISP-proxy httpx (pinned exit IP, rotate-on-failure)
          200 + real content    -> done   (the common Cloudflare case)
          all attempts fail      -> Tier 4
Tier 3  naked Playwright (full page render) — genuine SPA sites
          insufficient           -> Tier 4
Tier 4  ISP-proxy Playwright (pinned IP, image/css/font/media blocked) — last resort
          insufficient           -> raise (fetch_error)
```

For blocked cases, Tier 1 makes up to 3 quick attempts (httpx desktop →
requests/HTTP-1.1 desktop → httpx android) before declaring the IP blocked. A
200 that looks like a JS shell goes straight to naked Playwright because the
datacenter route is reachable but needs rendering. `READ_TIMEOUT` is 8 s so a
blocked IP fails fast. **AMP variants were dropped entirely** — they no longer
fool Cloudflare in 2026 and were the biggest time sink.

## Routing signals — no Cloudflare heuristics

Routing is driven only by:

1. **HTTP status / connection outcome** — if all naked attempts end in non-200,
   reset, or timeout, the datacenter IP is treated as blocked and the fetch moves
   to the proxy tier. A datacenter block shows up as 403 *or* a TCP reset, so
   both are treated the same. In the proxy tier, non-200s, resets/timeouts, and
   proxy errors rotate the pinned ISP session.
2. **`_has_sufficient_content(html)`** — a *structural* check (≥3 `<p>` tags and ≥300 chars
   of paragraph text in the **raw HTML**, which any real news page passes). Used to tell a
   real 200 from an empty SPA shell.

We deliberately do **not** text-match the body for Cloudflare phrases ("Just a moment",
"Sicherheitsüberprüfung"): those strings can legitimately appear in article prose. We
also do **not** gate on the *extracted* article length — real articles can be as short as
700–800 chars, the same size as a challenge page. Browser-rendered fallbacks
(Tier 3, Tier 4, and the no-proxy-creds fallback) are still gated through
`_has_sufficient_content`, so a challenge/shell render **raises `fetch_error` instead
of returning the junk**.

## The pinned ISP session

BrightData hands out a different exit IP per *session* string. Two strategies exist in
the codebase:

| | WELT / BILD (paywall) | General fetch (this ladder) |
|---|---|---|
| Session | static, in `paywalls.json` (`"welt"`, `"bild"`) | process-global, minted at runtime |
| Exit IP | one permanently warmed IP | one pinned IP, **rotated when flagged** |
| Rotation | up to 3 throwaway peers (4 IPs) on `ERR_TUNNEL_CONNECTION_FAILED` / no warm peer | on non-200 / insufficient content / proxy error |

Observed reality: about **a third to a half** of random ISP exit IPs are themselves
Cloudflare-flagged on a given site (and after pruning some countries from the zone, some
sessions also return `400 Peer not found`). So a fresh-random-IP-per-fetch strategy pays
that roulette on *every* fetch.

Instead, `fetch_html` keeps **one process-wide pinned session**:

```python
_isp_session: str | None = None       # the current pinned session ("rw" + hex)
_isp_lock = threading.Lock()          # the 6 parallel-domain workers share it
```

- **Warm before fetch**: `_ensure_warm(session, cfg)` calls the shared `_warm_proxy`
  (paywall handler) once per session before the first httpx fetch, so the session binds an
  exit IP. BrightData returns `400 Peer not found` until a peer is allocated for a new
  session; the warmup retries the *same* session until it binds (a different remedy than a
  flagged IP, which needs a new session). Result cached in `_warmed_sessions` so the pinned
  IP isn't re-warmed.
- **Steady state**: every fetch reuses the warmed `_isp_session` → same warm IP → one cheap
  attempt, matching the WELT/BILD reliability.
- **On failure**: `_rotate_isp_session(failed)` mints a new session (new IP) and retries,
  up to `PROXY_HTTP_ATTEMPTS` (6). Rotation is reserved for a *flagged/dead* IP (403,
  challenge, fetch-time proxy error); a warmup that finds no peer also rotates. The session
  that finally works stays pinned for all later fetches.
- **Compare-and-swap**: if another worker already rotated past the session I just saw
  fail, I adopt the current one instead of minting yet another — so a burst of parallel
  fetches hitting the same bad IP converges on **one** new IP, not many.
- **Scope/lifetime**: single global, **in-memory only**. A restart just warms one fresh IP
  on the first fetch; nothing is persisted.

Measured on `dorsten-online.de` after adding the warmup: the cold first fetch dropped from
~11 s (3–4 `400 Peer not found` retries) to ~2 s, and every subsequent fetch is attempt 1 /
~1 s on the pinned session.

## Paywall path — throwaway rotation on tunnel failure

The paywall handler (`paywall/handler.py:_playwright_fetch`) uses a *static* sticky session
per site (`-session-bild` / `-session-welt`) so the warmed login IP is reused. But a sticky
session keeps being handed the **same exit peer**, so when that peer goes bad Chromium's
`page.goto` fails with `net::ERR_TUNNEL_CONNECTION_FAILED` and every retry on the same
session re-hits the same dead peer. (Observed 2026-06-02: two sticky BILD retries 53 s apart
both failed with this error; only a later attempt — once BrightData had reassigned the
peer — succeeded.)

So each exit IP runs a **cheap→expensive ladder** and a tunnel failure rotates to a fresh
throwaway peer, up to `MAX_PROXY_ROTATIONS` (3) rotations = **4 exit IPs tried** (sticky +
3 throwaways):

```
per exit IP (sticky first, then throwaways):
  gate 1  requests warm  (_warm_proxy, 3× same session for 400 Peer not found)
            no peer binds          -> rotate (burns an IP slot, no browser launch)
  gate 2  Chrome 204 probe  (_probe_chrome_tunnel: page.goto gstatic/generate_204)
            ERR_TUNNEL_CONNECTION_FAILED -> rotate
  gate 3  article goto
            ERR_TUNNEL_CONNECTION_FAILED -> rotate
            success                 -> return HTML
  rotations exhausted / any non-tunnel error -> return "" (fetch_error)
```

- **Why a Chrome 204 probe.** `_warm_proxy` binds the sticky session to a peer over a
  *requests* connection, but Chromium opens its own tunnel and can still land on a dead
  peer. Navigating to gstatic's 204 on the page first proves *this* exit peer is live for
  Chrome (same session → same peer), so the article goto rides a proven peer. gstatic's 204
  has no body, so a successful round trip surfaces as `ERR_ABORTED` — `_probe_chrome_tunnel`
  swallows that and only lets a real `ERR_TUNNEL_CONNECTION_FAILED` propagate. Our own
  `_apply_article_request_blocking` (resource-type filter) lets the `document` request
  through, so the probe isn't self-aborted. The probe is tiny (TLS + empty 204).
- **The requests warm is now a gate, not fire-and-forget.** Its bool return is honored: if
  no peer binds it rotates before paying for a Chromium launch (but still consumes one of the
  4 IP slots so the loop can't spin forever).
- **Only `ERR_TUNNEL_CONNECTION_FAILED` triggers rotation.** `ERR_PROXY_CONNECTION_FAILED` is
  excluded on purpose — it means Chromium couldn't reach the superproxy host at all, so a
  new session (same host) wouldn't help. A `goto` timeout and a paywall stub are likewise
  not rotated (the stub has its own `_relogin` path; an empty/`""` result never reaches it).
- **Bounded loop, not a `rotated` flag** — a `rotations` counter (capped at
  `MAX_PROXY_ROTATIONS`) replaces the old single-shot boolean. Both the warm gate and the
  fetch failure share the same budget, so a mix of warm-fails and tunnel-fails still tries at
  most 4 distinct exit IPs. A sustained block past that is left to BrightData's ~daily IP
  rotation or a manual change of the static session.
- **Cause is preserved on give-up** — before returning `""`, the handler stashes the cause
  via `bandwidth.note_error(_error_cause(e))` (the `net::ERR_…` code, or a short type/message
  form). `scrape_server` reads it in the worker thread and folds it into the `fetch_error`
  `detail` (`empty result (net::ERR_TUNNEL_CONNECTION_FAILED)`), so the monitor shows the root
  cause instead of a bare `empty result`. Previously this lived only in the text service log.
- **Reuses `storage_state`** — login cookies aren't IP-bound, so the throwaway's fresh
  DE/FR/IT exit serves the saved session fine (no re-login).
- **Helpers**: `_session_of` (pull the session out of a `proxy_cfg`), `_swap_session`
  (clone the cfg with a new session = new peer), `_is_tunnel_error` (substring match on the
  single error code), `_probe_chrome_tunnel` (the gstatic-204 Chrome-tunnel probe).

**Monitor signal**: each attempt passes its real session to `bandwidth.set_proxy(name,
session)`, so a paywall `fetch_ok` row shows `proxy_session: bild` normally and
`proxy_session: rot1a2b3c` whenever a fetch was saved by a throwaway rotation — an
at-a-glance marker that the sticky peer blipped. The `rot` prefix is deliberately distinct
from the sticky `bild`/`welt` so it can't be skimmed past.

## Resource blocking — proxied renders only

`crawler_playwright.fetch_html_with_playwright(url, proxy_cfg=…, proxy_name="isp")` blocks
`image`, `font`, `media`, and `stylesheet` (keeping `document`, `script`, `xhr`/`fetch` so
SPA sites still render). This blocking is applied **only when a proxy is used**, because
ISP bandwidth is billed per GB. **Naked (datacenter) Playwright loads the whole page** —
its bandwidth is effectively free, and a full load is more robust for SPA rendering. The
function warms the BrightData tunnel (`_warm_proxy` against `gstatic/generate_204`) before
launching Chromium, mirroring the paywall handler, and its in-memory cache key includes
`proxy_name` so proxied and naked results for the same URL never collide. 

If Tier 4 shows 10 MB+ renders in the monitor (JS-heavy SPAs loading large bundles and third-party analytics/ad scripts), the two highest-leverage fixes are: (a) extend blocking to third-party `script` requests (keep only scripts from the article's own registered domain — mirrors the `_WELT_ARTICLE_SCRIPT_WHITELIST` approach in the paywall handler), and (b) add a hard wire-byte cap that aborts all further page requests once `bandwidth.consumed()` crosses a threshold (e.g. 3 MB), since partial HTML is usually sufficient for trafilatura on any real article.

## BrightData config

- Zone: `isp` (`isp_proxy1`), password env `BRD_PASS_ISP`; host `brd.superproxy.io:33335`.
- Proxy cfg / URL built by `paywall/handler.py:_proxy_cfg()` / `_proxy_url()` (shared with
  the paywall flow). General fetches pass a rotating `-session-rw<hex>`; paywall fetches
  pass their static `-session-welt` / `-session-bild`, rotating to throwaway
  `-session-rot<hex>` peers (up to 3) on a tunnel failure (see [Paywall path — throwaway rotation](#paywall-path--throwaway-rotation-on-tunnel-failure)).
- If `BRD_PASS_ISP` is unset, Tier 2/Tier 4 are unavailable and the ladder falls back
  to a naked Playwright render. That render still must pass `_has_sufficient_content`;
  otherwise `fetch_html()` raises.

## Run-log / monitor integration

- `bandwidth.set_proxy(zone, session)` records both the zone and the **session actually
  used** for the winning fetch (threadlocal, read in the scrape worker thread).
- `scrape_server._scrape_one` reads `bandwidth.proxy()` and `bandwidth.proxy_session()`
  and emits them on the `fetch_ok` row. The paywall handler now also calls
  `bandwidth.set_proxy(name, session)` with the session actually used, so a paywall row
  reports its real `bild`/`welt` — or a `rot…` throwaway after a rotation — rather than the
  `paywalls.json` fallback (`scrape_server` still falls back to the static session only when
  bandwidth reported none).
- **wire bytes / cost**: Tier 4 renders are CDP-instrumented; the Tier 2 httpx path isn't,
  so `_proxy_httpx` records its **compressed** wire size via `bandwidth.add(r.num_bytes_downloaded)`
  (summed across rotated/failed attempts too). Naked datacenter httpx stays `0` (free,
  unmetered). The monitor derives `wire_cost = wire_bytes × ISP $/GB` for both — so a Tier 2
  success now shows a real (small, ~50 KB / ~$0.0008) proxy cost instead of `0`.
- **article text**: `fetch_ok` also logs the extracted article `text` (capped at 32 KB,
  `MAX_LOGGED_TEXT`) so the monitor can show it as a hover column for fetch QA over time.

## Tunables

| Constant (`scraper_fetch_html.py`) | Default | Purpose |
|---|---|---|
| `READ_TIMEOUT` | 8 | naked datacenter read timeout (fail fast on a blocked IP) |
| `PROXY_READ_TIMEOUT` | 20 | ISP proxy read timeout (proxy adds latency) |
| `PROXY_HTTP_ATTEMPTS` | 6 | rotating ISP HTML attempts before a render (~94% success at the observed per-IP rate) |
| `_PROXY_ZONE` | `"isp"` | BrightData zone for the proxy tiers |

## Testing

`tests/manual_scrape_one_url.py` exercises the path two ways:

- `--ladder [url]` runs the integrated `fetch_html()` with verbose logging (shows which
  tier/attempt won and the pinned session).
- default / `--tiers 1,2,3,4 [url]` runs each tier **independently** and prints a
  comparison table (status, exit IP, raw KB, extracted chars, time) — useful for gauging
  per-IP success rate and confirming a datacenter block. These independent probes are
  diagnostics; use `--ladder` for the exact production control flow.

Run on the VPS with `env/bin/python3 tests/manual_scrape_one_url.py --ladder` (it loads
`.env` for `BRD_PASS_ISP`).
