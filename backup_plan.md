# Disaster recovery: laptop loss

The laptop at `c:\apps\brandmonitor` is the only host that collects. If it dies,
stops booting, or is stolen, collection stops and the loss is partly
unrecoverable. This document is the plan for moving the pipeline to the Contabo
VPS and back again.

It is a **manual** procedure. Automatic failover stays out of scope
(`mvp_plan.md`): a laptop that is merely asleep, travelling, or off Wi-Fi for a
day looks exactly like a dead one, and an automatic takeover would produce two
hosts writing divergent databases — the failure `CLAUDE.md` already forbids.

## The deadline that sets everything else

News sitemaps retain titles for roughly 48 hours (`todo.md` §5). Past that, the
titles are gone from the source and no later run can recover them.

| Quantity | Value | Consequence |
|---|---|---|
| RPO (data already lost at t=0) | up to 24 h | the day's collection since the last backup |
| RTO (target) | **under 48 h** | beyond it, title coverage is permanently reduced |

So the plan is judged by one question: can collection be running on the VPS
inside two days, by one person, from a hotel, with no access to the dead laptop?
Everything below either serves that or is marked optional.

---

## 1. What already works (verified on the VPS, 2026-09-12)

This is measured, not assumed. Much of the plan is already in place because
other projects on the same box needed it first.

| Element | State | Evidence |
|---|---|---|
| VPS capacity | 4 vCPU, 7.9 GB RAM, 4 GB swap, 129 GB free on `/` | `nproc`, `free -m`, `df -h` |
| `rclone` on the VPS | configured with an `onedrive:` remote at `/root/.config/rclone/rclone.conf` | `rclone listremotes` |
| Backups reachable from the VPS | **yes, but the wrong machine's** — see §1.1 | `rclone ls` byte-for-byte |
| Application code | `origin/main` == local `main`, nothing unpushed | `git rev-list --left-right --count` |
| Playwright browsers | installed system-wide: `chromium-1223`, `chromium-1228` | `/root/.cache/ms-playwright` |
| Headful browser display | `xvfb-99.service` enabled and running since 2026-09-11 | `systemctl status xvfb-99` |
| Paywall credentials | all five pairs already in `/var/www/brandmonitor/.env` | key names only |
| Bright Data credentials | `BRD_PASS_ISP`, `BRD_PASS_RESIDENTIAL`, `BRD_PASS_CHINA`, `BRIGHTDATA_*` already in that same `.env` | key names only |
| Proxy fetch ladder | solved and documented in another project | `c:\apps\rewriter\vendor\html_scrape_with_proxy.md` |
| Paywall-through-proxy | solved and running on this VPS | `rewriter-scrape.service` |
| Remote hands | Tailscale on both hosts; `claude-remote-control.service` running on the VPS | `systemctl` |

The mechanism works: the VPS can pull a database from OneDrive today, with no new
work.

```bash
rclone copy onedrive:brandmonitor-backups/brandmonitor-20260912.db.gz /tmp/
```

The backup chain is host → OneDrive client → Microsoft → VPS rclone. It does
**not** depend on the source host being alive at recovery time, only on it having
synced before it died.

### 1.1 The primary host had no off-box backup at all

**Found and fixed 2026-09-12.** The laptop's OneDrive client was signed in to the
wrong account; re-signing it in made the whole account appear and
`brandmonitor-backups` sync down. Recorded here in full because the *failure
mode* is not fixed by fixing this instance of it — see the verification note at
the end.

Two Windows machines carry a `c:\apps\brandmonitor` working copy:

| | `DESKTOP-PAF96VP` ("DELL Laptop") | `Jin_3060` (user `jinch`) |
|---|---|---|
| Role per `CLAUDE.md` | **primary**, scheduled pipeline | second workstation |
| Tailscale | `100.80.13.120` | — |
| Highest `run.id` (2026-09-12) | **49** | 41 |
| `data/brandmonitor.sqlite3` | 61,902,848 B | 62,386,176 B |
| `brandmonitor-20260912.db.gz` | **12,633,570 B** | **12,407,372 B** |
| Microsoft account | `jinchilu@hotmail.com` | `jinchilu@hotmail.com` |
| Reaches the cloud? | **no** | yes |

Both machines were configured for the *same* OneDrive account, so the natural
assumption — one folder, synced everywhere — was the right mental model and the
wrong description of this host. The laptop's client was actually signed in to a
different account, and the symptoms were:

- `C:\Users\DELL Laptop\OneDrive` contains exactly one entry,
  `brandmonitor-backups`. The account itself holds `Dokumente`, `Desktop`,
  `Obsidian`, `Bilder`, `brandchecker-backups`, `rechnung-backups` and more
  (`rclone lsd onedrive:`). A syncing client would show those.
- The laptop's 2026-09-12 snapshot (12,633,570 B, written 17:00:30) had still not
  appeared in the cloud an hour later.
- The cloud's 2026-09-12 entry is 12,407,372 B — `Jin_3060`'s snapshot.

The cause is in [src/backup.py:192](src/backup.py#L192): the off-box step does
`offbox.mkdir(parents=True, exist_ok=True)` before copying. `config.json` sets
`offbox_dir` to `~/OneDrive/brandmonitor-backups`, and on a host where OneDrive
is not actually syncing that path, the backup **creates an ordinary local
directory** and reports off-box success. `CLAUDE.md` states the assumption
plainly — "a plain directory the OneDrive client already syncs" — and the laptop
is the host where it does not hold.

So the primary's backups live on the primary's disk, twice. If it dies, they die
with it, and the only surviving copies are `Jin_3060`'s — which are a different,
staler database (`run 41` against `49`). Restoring those during a real incident
would silently roll the corpus back eight runs while every check in the runbook
passed.

**Resolved 2026-09-12** by signing the laptop's OneDrive client in to
`jinchilu@hotmail.com`. Verified: the account's other folders (`Dokumente`,
`Obsidian`, `brandchecker-backups`, `rechnung-backups`) now appear under
`C:\Users\DELL Laptop\OneDrive`, and `brandmonitor-backups` synced down as
Files-On-Demand placeholders. The first genuine off-box snapshot from the primary
is expected on the 2026-09-13 run.

**End-to-end verified the same day.** `Jin_3060`'s three snapshots were removed
from the shared folder and the laptop's own 2026-09-12 snapshot copied up, giving
one consistent chain:

```text
laptop  data/backups/               12,633,570
laptop  OneDrive/brandmonitor-backups   12,633,570
cloud   rclone lsl (read from the VPS)  12,633,570
```

The VPS now pulls the primary's real database. That is the check that matters —
*the cloud holds this host's snapshot at this host's byte size*, read from the
recovery host — and it is the check that was never being made.

Two things remain, and they are the durable part:

1. **Host-specific folders.** Both machines wrote `brandmonitor-<date>.db.gz`
   into one account, and for several hours the 09-12 entry was the *wrong*
   database for that date with nothing in the listing to say so. One machine
   writes there now, so the ambiguity is gone by convention rather than by
   design. `brandmonitor-backups-laptop/` makes it structural.
2. **A verification that fails loudly.** This went unnoticed because an off-box
   copy that silently becomes a local copy logs identically to a real one:
   `off-box C:\Users\DELL Laptop\OneDrive\brandmonitor-backups\...`.
   `mkdir(parents=True, exist_ok=True)` in
   [src/backup.py:192](src/backup.py#L192) will recreate a plain local directory
   for any future host whose sync is not what it looks like — the VPS in
   emergency mode included. The byte-size check above belongs in the health
   probe, not in someone's memory.

§10 remains open on whether to drop OneDrive from the primary's path altogether
in favour of a VPS pull over the existing SSH channel, which fails visibly rather
than silently.

### 1.2 The other weakness in the chain

OneDrive sync is a client on the dying machine. A laptop that fails at 14:00
uploaded its last snapshot at the end of the previous night's run, and anything
the client had queued but not yet pushed is lost with the disk. This is
acceptable — it is the same 24 h RPO stated above — but it means the OneDrive
copy is the recovery source, not the laptop's `data/backups/`.

### 1.3 Two machines are a standing divergence risk

`Jin_3060` is not a backup host. It has its own database that has been collecting
independently (`run 41` against the laptop's `49`), and it holds the only copy of
`data/safety_gate_validation.sqlite3`, `data/backfill/` and `data/body_validation/`.

This is the same hazard §8 forbids between the laptop and the VPS, and the same
rule applies: **one host collects.**

**Resolved 2026-09-12.** `Jin_3060`'s database and local snapshots were deleted,
making it a development checkout. Its validation artifacts —
`safety_gate_validation.sqlite3`, `backfill/`, `body_validation/` — were kept,
since they exist nowhere else and the conclusions drawn from them live in
`clients/jt-express/labels/` and `docs/selection_and_assessment.md`, both in git.

The rule this leaves behind: a second working copy is fine, a second *database*
is not. `run.py` writes to `data/brandmonitor.sqlite3` by default on any machine
with a checkout, so the way this recurs is someone running `collect` on a
development box without meaning to.

---

## 2. What is missing

Ranked by whether it stops the recovery.

### 2.1 Blocking

**`/var/www/brandmonitor` is a stale skeleton.** The checkout is from
2026-08-18 and contains only `CLAUDE.md`, `new_product_plan.md`, `vendor/`,
`env/` and `.env`. There is no `src/`, no `run.py`, no `migrations/`, no
`health/`, no `input/`, no `clients/`. A `git pull` fixes the tree, but the venv
is Python 3.12.3 against the laptop's 3.11 and has never had this project's
requirements installed.

**There is no `run_daily.sh`.** [run_daily.bat](run_daily.bat) is not a
convenience wrapper; it is the orchestration contract. It holds the single-writer
lock on `data\run.lock`, runs ten stages that deliberately do not chain on each
other's success, records a per-stage exit code, propagates the worst one, and
writes the `data/last_run.json` marker whose JSON shape
[health/check.py](health/check.py) validates strictly. None of that exists for
Linux, and it is the largest single piece of work in this plan.

**Two API keys are absent from the VPS `.env`.** Comparing key names against the
laptop's live file, the VPS has every paywall pair and every Bright Data
credential, and is missing exactly:

- `OPENAI_API_KEY` — without it the title gate, the body gate and the entire
  report stack are dead;
- `DSA_KEY` — without it `collect-dsa` raises rather than degrading.

`DIP_API_KEY` is absent from both and does not matter: [src/dip.py](src/dip.py)
falls back to the public key, which is valid until the end of May 2027.

This is a much narrower gap than expected, and it should be closed **now**, not
during an incident. Both keys are re-issuable from their consoles, but that is a
slow path to discover at 02:00.

### 2.2 Silent data loss

**The backup covers SQLite and nothing else.** [src/backup.py](src/backup.py)
snapshots `data/brandmonitor.sqlite3` through the online backup API and gzips it.
`data/` is gitignored wholesale. Therefore these exist only on the laptop and are
in no backup anywhere:

| Path | What it is | Cost of losing it |
|---|---|---|
| `data/reports/*/issue-register.json` | the unit of state between weeks (`CLAUDE.md`) | next week's carry-forward has no baseline; continuing stories restart from zero |
| `data/title_gate/*.jsonl` | retained title-gate decisions | `fetch-bodies --title-gate-client` loses its queue of keeps awaiting retry |
| `data/reports/<bundle>/` | frozen export bundles | past reports are unreproducible; `verify-report` cannot be re-run |

The fix is small: add the report bundles and the title-gate logs to the daily
off-box push. They are text, they compress, and the whole of `data/reports/` is
smaller than one database snapshot.

**Resolved 2026-09-13.** `run.py backup` now writes
`brandmonitor-state-<date>.tar.gz` (both directories) beside each snapshot, with the
same rotation and the same OneDrive copy. Measured on the laptop: 33.8 MB of
`reports/` and `title_gate/` compress to 8.0 MB in 1.6 s, so about 110 MB for 14
dailies.

### 2.3 Operational

**Nothing watches the VPS once the VPS is the pipeline.** The health checker is
built to watch the laptop from the VPS. In emergency mode the watcher and the
watched are the same host, so any failure that takes down the box also takes down
its own alarm.

**`offbox_dir` points at a path that does not exist on Linux.**
[config.json](config.json) sets `~/OneDrive/brandmonitor-backups`, which is a
synced folder on Windows and nothing at all on the VPS. In emergency mode the VPS
would keep local snapshots only, making it a new single point of failure.

**Resource contention.** The VPS is not idle. It runs `brandchecker.service`,
`rechnung.service`, `rewriter-scrape.service`, `rewriter-public-api.service`,
`rewriter-public-scrape.service`, `nginx`, `tailscaled` and
`claude-remote-control`, using about 2.0 GB of 7.9 GB at rest.
`rewriter-scrape` is already capped at `MemoryHigh=2.5G` / `MemoryMax=3G`.
brandmonitor's `workers.crawler = 20` plus Playwright escalation on top of that
is how the box gets OOM-killed and takes four other services with it.

---

## 3. The recovery runbook

Run in order. Steps 1–4 are the minimum to be collecting again.

### Step 0 — decide, and write it down

Emergency mode is a decision, not a threshold. Confirm the laptop is
*unrecoverable within the RTO*, not merely offline. Note the date and time; the
failback in §8 needs it.

### Step 1 — bring the checkout current

```bash
cd /var/www/brandmonitor
git pull --ff-only
python3 -m venv --clear env            # 3.12 is fine; the project has no 3.11 pin
env/bin/pip install -r requirements.txt
env/bin/python3 -c "import playwright; print('ok')"
```

Playwright's browsers are already at `/root/.cache/ms-playwright` and are shared
across venvs, so `playwright install` should not be needed. Verify rather than
assume — a version bump in `requirements.txt` can want a browser build that is
not there.

### Step 2 — complete the `.env`

Copy `OPENAI_API_KEY` and `DSA_KEY` into `/var/www/brandmonitor/.env`. Everything
else is already present. Keep the file at `chmod 600`.

### Step 3 — restore the database

Confirm first that the remote resolves to the **laptop's** OneDrive (§1.1). Check
the byte size against the laptop's own `data/backups/` listing if the laptop is
still readable; if it is not, the size must not match `Jin_3060`'s snapshot for
the same date.

```bash
rclone copy onedrive:brandmonitor-backups/brandmonitor-<YYYYMMDD>.db.gz /tmp/
mkdir -p /var/www/brandmonitor/data
gunzip -c /tmp/brandmonitor-<YYYYMMDD>.db.gz \
  > /var/www/brandmonitor/data/brandmonitor.sqlite3
rm -f /var/www/brandmonitor/data/brandmonitor.sqlite3-wal \
      /var/www/brandmonitor/data/brandmonitor.sqlite3-shm
env/bin/python3 run.py migrate
env/bin/python3 run.py status
```

Deleting the `-wal` and `-shm` sidecars is not optional. The database runs in WAL
mode; stale sidecars beside a restored file are replayed on next open and
silently undo part of the restore.

`run.py status` is the proof the restore worked — it should show the runs and
per-source outcomes from the snapshot's last day.

**This path has been exercised for real.** On 2026-09-12 the laptop's live
database was deleted by mistake — a cleanup command intended for `Jin_3060` run
in a laptop shell. Restoring from `data/backups/brandmonitor-20260912.db.gz`
returned `pragma quick_check` = `ok`, `max(run.id)` = 49, and a `run.py status`
identical to the pre-deletion state: **zero rows lost.**

Two properties made that outcome possible, and both are load-bearing rather than
incidental:

- **`backup` is the last stage of `run_daily.bat`.** The snapshot was taken at
  17:00:30, after `dip_documents` closed run 49 at 15:00:28, so it carried the
  whole day. A backup scheduled first would have lost it.
- **The snapshot is VACUUMed, so the restored file is smaller** (61,517,824 B
  from a 61,902,848 B original). Size is therefore *not* a check that a restore
  is complete; `max(run.id)` and `run.py status` are.

It also produced the trap §1.1 warns about, in its most literal form: the
laptop's `data/` root held a `brandmonitor-20260912.db.gz` of 12,407,372 B —
`Jin_3060`'s snapshot — beside the correct 12,633,570 B one in `data/backups/`.
Same name, same date, different database, and only the byte size distinguishes
them. Restoring the wrong one would have silently reverted the corpus by eight
runs.

### Step 4 — collect once, by hand, watching it

```bash
env/bin/python3 run.py collect --kind news --source <one fast source>
```

This is where a datacenter IP shows itself. Expect some publishers to return 403
or a bare TCP reset (§5). Do not proceed to a scheduled run until one manual run
completes and stores rows.

### Step 5 — schedule it

A cron entry mirroring the laptop's intended Task Scheduler job, once
`run_daily.sh` exists:

```cron
0 6 * * * cd /var/www/brandmonitor && ./run_daily.sh >> /var/log/brandmonitor-daily.log 2>&1
```

Cron is the right tool here rather than a systemd timer: the laptop's schedule is
a plain daily trigger, and the Windows settings that matter
(`StartWhenAvailable`, the battery flags) exist because a laptop sleeps. A VPS
does not, so there is nothing to reproduce, and cron is one line that survives a
reboot. If the contention in §2.3 shows up in practice, wrap it:
`systemd-run --scope -p MemoryMax=2G ./run_daily.sh`.

### Step 6 — switch the health check to emergency mode (§6)

### Step 7 — restore the report state

Pull the same date's state archive (§2.2) and extract it into `data/`:

```bash
rclone copy onedrive:brandmonitor-backups/brandmonitor-state-<YYYYMMDD>.tar.gz /tmp/
tar -xzf /tmp/brandmonitor-state-<YYYYMMDD>.tar.gz -C /var/www/brandmonitor/data
```

Use the date that matches the restored database. If no state archive exists for
it, the first weekly report after the incident starts with an empty issue
register — say so in the report rather than presenting a fresh baseline as
continuity.

---

## 4. Fetching from a datacenter IP

The laptop fetches from a residential connection. The VPS does not, and this is
the single behavioural difference that most affects collection quality.

`rewriter` already solved this on **this exact VPS**, and the finding is
documented in `c:\apps\rewriter\vendor\html_scrape_with_proxy.md`. The measured
facts:

- many publishers sit behind Cloudflare, which blocks Contabo outright — as
  **HTTP 403 *or* a bare TCP reset**, so both must be treated as the same signal;
- a headless render on the same datacenter IP does **not** help: it loads the
  Cloudflare interstitial (~30 KB, a few hundred characters of junk) and, if
  trusted, gets stored as though it were the article;
- a Bright Data ISP exit IP returns HTTP 200 with the full article;
- about **one third to one half** of random ISP exit IPs are themselves flagged
  on a given site, so a fresh IP per fetch pays that roulette every time.

### What to port, and what not to

brandmonitor's vendored crawler is the older lineage. It has no proxy support
anywhere: not in
[scraper_fetch_html.py](vendor/newscrawler/scraper_fetch_html.py), not in
[crawler_playwright.py](vendor/newscrawler/crawler_playwright.py) (whose
signature is `(url, timeout_ms, use_cache)` against rewriter's
`(url, timeout_ms, use_cache, proxy_cfg, proxy_name, url_guard)`), and not in
[paywall/handler.py](vendor/newscrawler/paywall/handler.py).

Its current Bright Data support is the *wrong shape* for this purpose:
`fetch_html` short-circuits to the Unlocker API when a source carries
`"brightdata": true`, and raises if that fails rather than falling back
([scraper_fetch_html.py:263](vendor/newscrawler/scraper_fetch_html.py#L263)).
That is a per-source replacement. Emergency mode needs the opposite — an
escalation rung reached on evidence, for sources that were never flagged.

Port the ladder, not the whole file — and **not all of it**:

```text
Tier 1  naked httpx (datacenter IP)
          200 + real content        -> done
          200 + JS/SPA shell        -> Tier 3
          403 / reset / timeout     -> Tier 2
Tier 2  ISP-proxy httpx, pinned session, rotate on failure
          200 + real content        -> done        (the common Cloudflare case)
Tier 3  naked Playwright            -> genuine SPA sites (datacenter IP, unmetered)
Tier 4  ISP-proxy Playwright        -> NOT BUILT. See below.
```

### Tier 4 is deliberately out of scope

Decided 2026-09-12. Tiers 1–3 are built; a page that needs a **proxied browser
render** is dropped rather than fetched.

The economics make this easy. Tier 2 is about 50 KB and ~$0.0008 per successful
fetch — at the observed ~157 body fetches a day, routing *every* one of them
through the proxy would cost roughly $0.13/day, which is not worth a decision.
Tier 4 renders were measured at 10 MB and up on JS-heavy pages, two orders of
magnitude more bandwidth on a per-GB zone, and it is the tier that needs the
resource-blocking machinery, the CDP byte instrumentation and a wire-byte cap to
stay safe. That is a lot of code to write and tune for a mode that is meant to
last days.

Tier 3 stays because it costs nothing: it runs on the datacenter IP, which is
unmetered, and a full page load is more robust for genuine SPA sites. It just
will not rescue a Cloudflare-blocked one — a naked render on a blocked IP paints
the interstitial, not the article, which is why Tier 3 must still be gated
through `_has_sufficient_content` and must raise rather than return.

What this gives up: a site that is **both** Cloudflare-blocked and
client-rendered has no path at all. Those bodies fail for the duration. Accepted
— but it has a consequence that is not obvious, below.

### The consequence: retired URLs

A dropped body is not a no-op. [src/bodies.py:829](src/bodies.py#L829) counts
consecutive failures, and at `body_fetch.max_attempts` (5, in
[config.json](config.json)) it **retires** the URL as `unavailable`:

```python
if result.status == "failed" and attempts >= BODY_FETCH_MAX_ATTEMPTS:
    result = BodyResult("unavailable", ...,
                        error=f"gave up after {attempts} attempts: ...")
```

Only `--retry-unavailable` or a changed discovery hint reopens it. So a DR period
lasting five daily runs permanently retires every URL on a blocked, JS-rendered
site — including URLs that the laptop, on its residential connection, would have
fetched without trouble. The laptop comes back, and those articles are simply
never attempted again.

This is silent and would not be noticed. The remedy is one step in the failback
(§8): a `fetch-bodies --retry-unavailable` pass after the restore. It reopens
legitimately-unavailable rows too (404s, declared paywalls), which costs one
wasted attempt each and is the cheaper mistake by a wide margin.

Three details in that document are the ones that took the measuring, and porting
without them reproduces the original bug:

1. **Route on status and structure, never on text.** Matching the body for
   "Just a moment" or "Sicherheitsüberprüfung" is wrong — those strings appear in
   real German article prose. Use `_has_sufficient_content`: at least 3 `<p>`
   tags and 300 characters of paragraph text in the raw HTML.
2. **Pin one warmed ISP session process-wide, rotate only when flagged.** A new
   session returns `400 Peer not found` until a peer binds, so warm it once
   against `https://www.gstatic.com/generate_204` and cache it. Measured effect:
   a cold fetch dropped from ~11 s to ~2 s.
3. **Gate the browser rungs through the same content check.** A challenge page
   that renders must raise, not return. [src/bodies.py](src/bodies.py) already has
   the right instinct — it treats "browser loaded a blank document" as a
   navigation failure — but the Cloudflare interstitial is not blank, it is a full
   page of the wrong content, and today it would be stored.

Bright Data config, already present in the VPS `.env`:

| Setting | Value |
|---|---|
| Host | `brd.superproxy.io:33335` |
| Customer | `brd-customer-hl_8bdecc95` |
| ISP zone | `isp_proxy1`, password `BRD_PASS_ISP` |
| Residential zone | `residential_proxy3`, password `BRD_PASS_RESIDENTIAL` |

Tier 2 is cheap — roughly 50 KB and ~$0.0008 per successful fetch. Tier 4 is not,
which is why resource blocking applies to proxied renders only; naked datacenter
rendering is unmetered and a full load is more robust for SPAs.

### Measure before porting

Even Tier 2 should be justified before it is built. Run collection against a
representative slice of `input/germany_medias.json` from the VPS and count how
many sources actually return 403 or a reset. It may be four publishers, in which
case marking those four and dropping them is cheaper than a proxy tier.
`rewriter` ships a diagnostic for exactly this —
`tests/manual_scrape_one_url.py --tiers 1,2,3,4 <url>` prints status, exit IP,
raw KB, extracted characters and time per tier — and it can be pointed at
brandmonitor URLs without porting anything first.

---

## 5. Paywalls

Three of the five work from the VPS with no proxy and no extra work. The other
two are dropped for the duration.

| Site | Route from the VPS | In emergency mode |
|---|---|---|
| spiegel.de | direct, no proxy | **kept** |
| zeit.de | direct, no proxy | **kept** |
| manager-magazin.de | direct, no proxy | **kept** |
| bild.de | Bright Data ISP, sticky session `bild` | **dropped** |
| welt.de | Bright Data ISP, sticky session `welt` | **dropped** |

### bild.de and welt.de are out of scope

Decided 2026-09-12, for the same reason as Tier 4 (§4) and one more. They are the
only two publishers needing a *proxied browser render*, which is the expensive
mechanism; and on their own signal contribution they do not repay the code —
porting `proxy` / `proxy_session` support into
[paywalls.json](vendor/newscrawler/paywall/paywalls.json) and the handler, to
save one or two days of low-signal coverage, is the definition of going overboard
for a temporary mode.

This is a *scope* decision, not a claim that it cannot be done: it is proven
working on this VPS today under `rewriter-scrape.service`. If an outage ever runs
long enough that two weeks of missing WELT and BILD start to matter, the port is
sitting there ready. Note what to carry over if that day comes:

- **Sticky, never rotating, on article fetch.** `ERR_TUNNEL_CONNECTION_FAILED` is
  a Chrome-level tunnel hiccup, not a bad peer. Rotating makes it worse; waiting
  8 s and retrying the same peer (4 attempts, 24 s maximum) recovers it.
- **Rotate the exit IP only in the login path,** when a paywall stub persists
  *after* a re-login — that is the login page rejecting the IP, a different
  failure entirely.
- **Residential does not work for WELT** — certificate errors without bypass, a
  broken login form with it. Keep both on ISP.
- **Xvfb is a hard dependency.** Headful Playwright login needs the display on
  `:99`. It is enabled and running on the VPS, but if `xvfb-99.service` is ever
  removed while tidying up `rewriter`, brandmonitor's paywall logins break with an
  error that looks nothing like a display problem.

Account safety, if the two are ever added: five simultaneous logins from a new IP
are what lockout heuristics look for on real subscriptions. The sticky session is
what avoids it — the exit IP stays constant, so the account does not appear to
teleport. Do not "simplify" it to a rotating proxy during an incident.

**What dropping them costs.** Selected items from these two are a handful a day,
and the §4 retirement rule applies to them as it does to any other failing fetch:
five consecutive daily runs retire the URL. The failback `--retry-unavailable`
pass (§8) covers this case too, so the coverage returns with the laptop.

The three direct sites need no configuration change at all — they work from the
datacenter IP exactly as they do from the laptop.

---

## 6. Health checking in emergency mode

Most of it already exists. [health/check.py](health/check.py) takes
`--marker-file`, which reads a local marker instead of SSHing to the laptop. It
was built for testing and happens to be exactly the emergency-mode switch.

```bash
python3 health/check.py --marker-file /var/www/brandmonitor/data/last_run.json
```

Note the timer is **not installed** on the VPS today —
`brandmonitor-health.timer` does not exist as a unit, which matches the README's
instruction not to enable it before the laptop's daily task is live.

Two behaviours need changing for emergency mode, both in `evaluate()`:

- **The laptop probe must be off, not merely failing.** Left pointed at the
  laptop it reports `unreachable`, which during a laptop loss is true, useless,
  and permanent.
- **The one-email-per-incident latch becomes a liability.** `notified_kind`
  suppresses repeats so an ongoing failure does not mail every fifteen minutes.
  That is right in steady state and wrong in DR, where silence is ambiguous —
  you cannot tell a healthy VPS from a dead one. Emergency mode should send a
  daily digest regardless of transition.

**Nobody watches the watcher.** Options, cheapest first:

1. A dead-man's-switch ping to a free external service after each successful run.
   Two lines in `run_daily.sh`, and the only option that survives the VPS itself
   being down.
2. Accept it, and rely on the weekly report visibly not arriving. Detection lag:
   up to seven days. Not acceptable against a 48-hour title window.

Option 1, at incident time if not before.

---

## 7. Backups while in emergency mode

Do not leave the VPS as an unbacked single point of failure. The mechanism is
already there — `rclone` with an authorized `onedrive:` remote — and
`c:\apps\brandchecker\deploy\scripts\install_backups.sh` is a working template for
a cron that snapshots SQLite, gzips, rotates and pushes to OneDrive.

For brandmonitor the Python command already does the snapshot and rotation, so
only the destination changes: set `backup.offbox_dir` to a local staging path and
add an `rclone copy` after it. That is simpler and has fewer failure modes than an
rclone mount.

Use a distinct remote folder — `onedrive:brandmonitor-backups-vps/` — so a VPS
snapshot can never be mistaken for a laptop snapshot during failback.

---

## 8. Failback

This is the direction nobody plans, and the one that can corrupt data.

**Rule: the VPS database wins wholesale. The two databases are never merged.**

Item ids are integers and the report stack resolves links by id — the writing
step emits `[文字](item:24617)` and the renderer substitutes the URL the export
froze. Two databases that both kept collecting have overlapping id ranges
describing different articles. Merging them does not produce a conflict; it
produces a report whose citations silently point at the wrong sources, which is
precisely the failure the id-resolution design exists to make impossible.

So the interlock matters: **while emergency mode is on, the rebuilt or repaired
laptop must not run collection.** If the laptop comes back with a usable disk, its
post-incident rows are discarded, not reconciled.

Failback procedure:

1. Stop the VPS cron entry. Confirm no run is in flight (`data/run.lock`).
2. Run `python3 run.py backup` on the VPS and push it to OneDrive.
3. On the laptop: restore that snapshot to `data/brandmonitor.sqlite3`, delete the
   `-wal` and `-shm` sidecars, run `run.py migrate`, then `run.py status`.
4. Copy back `data/reports/` and `data/title_gate/`.
5. **Reopen the URLs the outage retired** (§4):

   ```powershell
   .venv\Scripts\python run.py fetch-bodies --retry-unavailable
   ```

   Do this on the laptop's own connection, where the sites that were blocked from
   the datacenter fetch normally. Skipping it leaves a hole in the corpus that
   nothing later reports.
6. Run one manual `run_daily.bat` and confirm the marker is fresh and all stages
   exit 0.
7. Point the health check back at the laptop (drop `--marker-file`).
8. Only then re-enable the laptop's schedule.

Step 6 before step 8, always. A laptop that is scheduled before it is proven runs
unattended into whatever broke it.

---

## 9. What to do now versus at incident time

The distinction that matters: anything requiring a *working laptop* must be done
before the laptop stops working.

### Do now — cheap, and useless if deferred

- [x] Get the laptop's snapshots off the laptop (§1.1) — OneDrive re-signed in,
      verified from the VPS at 12,633,570 B, 2026-09-12.
- [x] Retire `Jin_3060`'s snapshots from the shared folder (§1.1).
- [x] Retire `Jin_3060`'s database so it cannot drift further (§1.3) — it is now
      a development checkout. Its validation artifacts stay.
- [x] Confirm the same byte-size check passes unattended on the 2026-09-13 run —
      `brandmonitor-20260913.db.gz`, 12,805,152 B, written 06:09 and listed from
      the VPS the same day.
- [ ] Move to a host-specific cloud folder, `brandmonitor-backups-laptop/`.
- [ ] Decide `Jin_3060`'s status (§1.3) — development checkout or second
      pipeline. If the former, remove its database so it cannot drift further.
- [ ] Add `OPENAI_API_KEY` and `DSA_KEY` to `/var/www/brandmonitor/.env`.
- [ ] `git pull` the VPS checkout to current `main` and build the venv, so a
      three-week-stale skeleton is not discovered during an incident.
- [x] Extend the off-box push to `data/reports/` and `data/title_gate/` (§2.2) —
      the state archive, 2026-09-13. First off-box copy is the next 06:00 run after
      the laptop pulls.
- [ ] Write `run_daily.sh` and run it once on the VPS against a restored snapshot
      in a throwaway directory. This is the real test of this whole document.
- [ ] Keep an encrypted copy of the laptop `.env` somewhere that is not the
      laptop. The VPS copy covers most of it, but the laptop is currently the only
      place all of it exists in one file.

### Do at incident time

- [ ] Steps 1–7 of §3.
- [ ] Install the emergency health timer.
- [ ] Point backups at `onedrive:brandmonitor-backups-vps/`.

### Build if measurement justifies it

- [ ] The **Tier 2** ISP-proxy HTTP fallback (§4). Measure the datacenter block
      rate first; if it is a handful of sources, per-source handling is cheaper
      than porting the ladder. Tier 4 is out of scope by decision.
Paywall proxy support for bild.de and welt.de is **decided out** (§5), not
pending measurement. The other three need nothing.

---

## 10. Open decisions

**Should the daily backup push straight to the VPS?** Largely settled by §1.1:
OneDrive is proven on `Jin_3060` and proven *not* working on the primary, and it
failed in the one way that leaves the log looking healthy. A direct copy over the
existing Tailscale channel removes Microsoft from the recovery path, shrinks the
RPO to the last completed run, and — the real argument — fails visibly when it
fails. What remains open is only whether OneDrive stays as a second copy
alongside it.

**How much of `rewriter`'s crawler should be adopted permanently?** The proxy
ladder is better than brandmonitor's vendored copy regardless of which host runs
it: the AMP fan-out it removes was ~85 s of stacked timeouts per blocked site.
Re-vendoring the newer lineage is a larger change than DR requires and should be
argued on its own merits, not smuggled in through a backup plan.
