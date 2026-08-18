# Brand & Reputation Monitor — Product Plan

Working document. Captures decisions made so far, the reuse map across the three
existing repos, the proposed architecture, and the questions still open.
Status: **pre-build — no code written yet.**

---

## 1. The product

A Chinese manufacturer/supplier whose brands are sold in Germany across multiple
retail platforms needs reputation and PR monitoring: what is being said about
them across news media, social media, and niche/vertical media.

| | |
|---|---|
| Customer | Chinese company, supplier of consumer brands sold in Germany |
| Use case | Reputation + PR management (not legal/regulatory risk) |
| Sources | ~50 customer-identified websites + social platforms |
| Cadence | Biweekly report |
| Deliverable language | Chinese |
| Competition | Traditional agencies, €2–5k/mo, slow |
| Our edge | One-man operation, automated pipeline, Doubao for Chinese output, transparent pass-through costs |

**Explicitly not in scope:** legislation / government tracking. The L1–L7 risk
layer model from germany_risk_monitor does not carry over.

---

## 2. Decisions made

| # | Decision | Notes |
|---|---|---|
| D1 | Hard-to-scrape social platforms are covered by **paid manual labor** (students, hourly) | Agreed with customer. Billed as **pass-through, zero margin**, fully transparent. |
| D2 | BrightData used where it works; manual labor where it doesn't | Per-platform choice, not all-or-nothing |
| D3 | Entity-based filtering, not topic-based | Per-client entity dossier; competitor monitoring is a near-free upsell |
| D4 | Real datastore from day one, not JSONL-only | See §5.2 — decision on which store still open |
| D5 | Multi-tenant structure from day one | `clients/<name>/` even with a single customer |
| D6 | New repo, build fresh, copy the plumbing | Do not extend rewriter or germany_risk_monitor in place |
| D7 | **SQLite, self-hosted. No Supabase.** | DB lives with the pipeline, backed up to the existing VPS |
| D8 | **Pipeline runs on a home laptop (Windows), not the VPS** | Residential IP removes the reason the proxy stack exists — see §5.5 |

**D1 is architecturally load-bearing.** Manual collection is not a side channel —
it is a connector like any other (§5.1). Human-collected mentions land in the same
record schema and flow through dedup, sentiment, clustering, and the report
identically. This keeps the pipeline uniform and means a platform can migrate from
manual → automated later without touching anything downstream.

---

## 3. Scope

### In scope (v1)
- ~50 customer-identified websites (news, trade press, niche/vertical media)
- Brand-term search discovery (not just per-site crawl) as a second recall path
- YouTube (Data API v3 — free, includes comments)
- Reddit (cheap API)
- FB / IG / TikTok / X via BrightData datasets **or** manual labor per platform
- Biweekly Chinese-language report
- Entity dossier per client (brands, aliases, products, executives, retail partners, negative keywords)

### Out of scope (v1)
- Legislation / regulatory tracking
- Real-time monitoring (see §7 for the daily-alert tier as a deliberate exception)
- Customer-facing self-serve portal
- Languages beyond DE source / ZH output

### Undecided — see §9
- Whether the "50 websites" are media outlets or e-commerce/review sites. This
  materially changes the build.

---

## 4. Reuse map

**Two** repos contribute. The split is not intuitive — the best fetch layer and the
best discovery layer live in different places.

| Source repo | What to take | Why |
|---|---|---|
| **rewriter** (`c:/apps/rewriter`) | `vendor/newscrawler/` fetch + paywall layer, `rewriter/api.py`, JSONL run-log + `monitor.py` | Newest, most patched fetcher. `handler.py` 510 lines vs. 175 in GRM; `scraper_fetch_html.py` 449 vs. 358. Has the escalation ladder, ISP-proxy pinning, bandwidth accounting, tunnel-error rotation. |
| **germany_risk_monitor** (`c:/apps/germany_risk_monitor`) | `crawler.py`, `crawler_google_feeds.py`, `parallel_crawler.py`, `source_loader.py`, `crawler_html_utils.py`, `assessment_agent.py`, `embedding_agent.py`, `llm_cost_calculator.py`, `logger.py`, `config.py`, `mocker.py`, `health_check.py`, watermark/history pattern, `word_report.py` as a docx *pattern* | rewriter has no discovery at all (users paste URLs). This is where discovery, batched triage, dedup, cost tracking and ops hygiene live. |

`c:/apps/NewsCrawler` (the pre-GRM Southeast Asia project) is not used — GRM is its
descendant and supersedes it.

### Vendoring approach
Discovery from **GRM**, fetch + paywall from **rewriter's vendor**. The two overlap
on `scraper_fetch_html.py`, `scraper.py`, `crawler_playwright.py`, `paywall/` — take
rewriter's for all of these, it is ~95 lines ahead. Restore `crawler_brightdata.py`
(rewriter's vendor dropped it, per [vendor/README.md](vendor/README.md)).

**One merge item:** GRM's `scraper_fetch_html.py` has four functions rewriter's fork
does not — `_amp_variants`, `should_try_amp`, `_attempt_httpx`, `_attempt_requests`.
rewriter dropped AMP fallback because it targets 5 known German majors. This product
hits ~50 heterogeneous small/niche sites where AMP variants are common, cheap and
often lighter-walled. **Evaluate porting AMP fallback forward** rather than
discarding it with the rest of GRM's fetch layer.

### Drop entirely
`crawler_gov/`, `main_gov_de.py`, `main_report_de.py`, the L1–L7 layer model, the
FAISS risk-prompt memory banks, `layer3_risk_prompts.json`.

---

## 5. Architecture

### 5.1 Source registry with heterogeneous connectors

germany_risk_monitor's source config assumes `sitemap / feeds / frontpage`. With 50
mixed-quality sites plus social plus humans, sources need a `connector` field:

```
sitemap              # GRM crawler.py — sitemap BFS with date-hint pruning
rss                  # plain feed
google_news          # GRM crawler_google_feeds.py — per-site AND per-brand-term
playwright_list      # SPA / JS-rendered listing pages
api:youtube          # Data API v3, includes comments
api:reddit
provider:brightdata  # per-platform dataset pull
manual               # human-collected — see below
```

This polymorphism is the main new abstraction. Everything downstream — dedup,
assess, extract, sentiment, cluster, store, report — is connector-agnostic and
operates on one normalized record schema.

**The `manual` connector (D1).** Requires three pieces:
1. **Task generation** — each cycle the system emits a work list per platform:
   which brand terms to search, which accounts to check, what date window.
2. **Intake** — a simple structured form or CSV that normalizes to the same record
   schema (url, platform, author, timestamp, text, engagement metrics).
3. **QC pass** — dedup against machine-collected records, plus a light validity
   check before the record enters the pipeline.

Records carry a `collection_method` field (`auto` / `manual`) so the report
appendix can be transparent about provenance — consistent with the zero-margin,
fully-disclosed pricing stance.

### 5.2 Datastore — SQLite (D7)

germany_risk_monitor appends JSONL and loads the entire URL set into memory for
dedup. That degrades across 50 sites plus social over a year, and it cannot answer
the queries the customer will ask ("all Brand X negative mentions in Q2", "is this
cycle worse than last").

**SQLite**, WAL mode, FTS5 for full-text. Single file, zero ops, self-hosted.
JSONL stays as the export format. Built fresh — no existing persistence layer is
reused.

Backup — mechanics matter:
- **Never file-copy a live SQLite DB.** In WAL mode a naive copy misses the `-wal`
  file and yields a silently stale or corrupt snapshot. Use
  `VACUUM INTO 'snapshot.db'` — consistent and compact even while the DB is in use.
- Push the snapshot to the VPS after each run.
- Keep **rolling daily snapshots (~30d)**, not one. A corrupted DB faithfully
  replicated over the only backup is the classic failure.
- Consequence: the laptop is disposable. Pull the DB down and run the pipeline
  anywhere.

### 5.3 Entity dossier + multi-tenancy

Per client: `clients/<name>/entities.json`, containing brand names and all
transliteration/spelling variants, product lines and model numbers, executive
names, retail partners and marketplace seller names, and **negative keywords**.

This is the highest-leverage config artifact in the product and the main source of
both false positives and false negatives. Chinese brands in the German market have
real naming chaos: Latin transliteration variants, umlaut spellings, seller names
that differ from brand names, and generic-word brand names that match everything.
Build it with the customer, refine it every cycle.

Multi-tenant layout from day one — costs nothing now, and the business only works
at several customers.

### 5.4 Pipeline stages

```
1. Discover   per-connector; site crawl + brand-term search = two recall paths
2. Triage     batched title/snippet entity match — keyword prefilter + LLM confirm
3. Extract    vendor fetch ladder → trafilatura/BS4  (auto sources only)
4. Ingest     manual-connector records join here, same schema
5. Dedup      embedding-based, cross-source and cross-cycle
6. Analyze    sentiment + reach/severity scoring + theme clustering
7. Store      persist to datastore, watermark advance
8. Report     Doubao → Chinese docx
```

Triage is *cheaper* here than in germany_risk_monitor: "does this mention a watched
entity?" is a keyword prefilter plus an LLM confirm, far less work than topical risk
classification.

Threading model carries over unchanged from GRM: `ThreadPoolExecutor` with
per-domain locks, HTTP serialized per domain, LLM calls free-running.

### 5.5 Deployment — home laptop + VPS (D8)

| Host | Role |
|---|---|
| **Home laptop** (Windows, 24/7) | crawl, scrape, extract, LLM calls, report generation, SQLite primary |
| **Contabo VPS** (€5/mo, existing) | backup target, heartbeat monitor, delivery endpoint, disaster-recovery runner |

**Work split — what runs where, and when.** Both hosts run brandmonitor code, so the
boundary has to be explicit rather than assumed:

| | Home laptop | Contabo VPS |
|---|---|---|
| **Scheduled** | full pipeline (discover → report) | heartbeat watcher, backup retention |
| **On demand** | any stage, during development | DR pipeline run, once the laptop is lost |
| **Never** | — | **scheduled pipeline run** |

The last row is load-bearing. Two hosts running cycles against one DB means duplicate
LLM spend and a diverged store, and it fails silently — nothing crashes, the numbers
are just wrong. Enforce it in code rather than in discipline: `run.py` refuses to
execute a cycle unless an env var marks the host as primary. The VPS clone never sets
it; a DR run overrides it explicitly, by hand, once.

**Why the laptop wins: the residential IP.** The entire proxy stack in
[vendor/newscrawler/](vendor/newscrawler/) — escalation ladder, pinned ISP session,
`ERR_TUNNEL_CONNECTION_FAILED` rotation, `MAX_PROXY_ROTATIONS`, per-GB BrightData
billing — exists to compensate for Contabo's IP looking like a bot. A consumer ISP
IP needs no compensation. Naked httpx mostly just works.

Paywall logins likely get *more* reliable: Welt/Bild are on ISP proxy because
subscription login from a datacenter IP looks like credential abuse. From a German
household IP it looks like a subscriber.

Volume is trivially safe — 50 sites biweekly is a few thousand URL checks and a few
hundred fetches per cycle, less than an hour of human browsing.

**Keep the proxy code, off by default.** No rotation is possible on a home IP, so it
is the only fallback if a site blocks it. Socials keep BrightData where used.

**Windows specifics:**
- Both codebases are already Windows-native (`chcp 65001`, `sys.stdout.reconfigure`).
- No Xvfb needed — a real desktop session runs headful Playwright natively. The VPS
  needs `xvfb-99.service`; the laptop does not.
- **Task Scheduler trap:** "Run whether user is logged on or not" executes in
  session 0 with no desktop → headful Playwright breaks. Enable auto-login and run
  the task **as the logged-in user**.
- Power: never sleep, never hibernate, "do nothing" on lid close. Ensure airflow.
- Defer Windows Update reboots. The watermark pattern makes mid-run kills survivable
  regardless.

**Remote access:** Tailscale — free, WireGuard, works behind CGNAT with no port
forwarding, stable tailnet address, all traffic outbound. RDP or SSH from anywhere.
*Open item:* confirm whether the line is DS-Lite/CGNAT (no inbound possible, common
on German consumer lines) — if so Tailscale is the only option rather than merely
the easiest. Telekom's daily forced reconnect rotates the IP every 24h, a free bonus
for crawling.

**Heartbeat:** laptop pings the VPS after each run; VPS alerts if none in N hours.
Both halves already exist — `health_check.py` (GRM) and the `/log` sink in
[scrape_server.py](scrape_server.py). This is also the bus-factor answer (§9).

**Deploy: pull, not push. No CI/CD.** GitHub's runners cannot reach the laptop behind
CGNAT, and auto-deploying to the VPS would mean parking an SSH key in GitHub secrets
to keep a *standby* current. Both hosts pull instead: `git pull --ff-only` (never a
bare `pull` — a stray local edit opens a merge and leaves stale code running), deps
reinstalled if `requirements.txt` moved, and pending migrations applied automatically
at `run.py` startup so a pull can never leave the schema behind the code.

The scheduled run does **not** pull. Deploying is a deliberate act performed at the
keyboard, so an unattended 3am run never executes code nobody watched start. If that
gate is later wanted without the manual step, pin the laptop to a `release` branch and
push `main:release` to ship.

CI is worth adding once there is code — `compileall` plus `pytest` on push catches the
syntax-error-at-3am class, where the run silently no-ops and the only symptom is a
report that never arrives.

`.env` is gitignored and therefore never deploys. It lives separately on each host and
drifts silently; the backup job should push it alongside the DB snapshot so the DR
path is genuinely one command instead of a credential scavenger hunt.

---

## 6. Report structure

Doubao for Chinese output is a genuine cost edge over anyone running GPT/Claude on
the write step.

1. Executive summary
2. Volume + sentiment vs. previous period
3. Notable mentions, ranked by reach × severity
4. Emerging narrative themes (embedding cluster → LLM names each cluster)
5. Competitor comparison (if in scope for that client)
6. **Recommended actions** ← what PR people actually pay for
7. Appendix: full mention list with links and `collection_method`

Everything above §6.6 is evidence for §6.6.

`word_report.py` from germany_risk_monitor (646 lines) is the docx plumbing
reference — structure is legislation-shaped and gets replaced, mechanics carry over.

---

## 7. Daily alert tier

A crisis does not wait 14 days. Run the same pipeline daily with a spike threshold,
email only on trigger. Near-zero marginal cost on top of what is being built, and it
is precisely what the traditional providers are bad at. Sell as a differentiator on
top of the biweekly base.

This is the one deliberate exception to "no real-time" in §3.

---

## 8. Cost model

Per client, per biweekly cycle:

| Line | Estimate | Notes |
|---|---|---|
| Discovery / crawl | ~€0 | Sitemaps and feeds are free |
| Triage LLM | negligible | Doubao, batched |
| Full scrape | ~€0 | Residential IP (D8) — no proxy bandwidth for sites |
| Analysis (sentiment/cluster) | small | Doubao |
| Report write | small | One large Doubao call |
| **Subtotal, automated** | **€20–60/cycle** | |
| Contabo VPS | €5/mo | Backup + heartbeat only |
| Home laptop | ~€3–5/mo power | Hardware already owned |
| BrightData — **socials only** | €0–300/mo | Sites no longer need it (D8) |
| **Manual labor (D1)** | **a few hundred €/mo, pass-through** | Zero margin, disclosed to customer |

All-in per client plausibly €250–700/mo against incumbents at €2–5k/mo. D8 removed
the site-proxy line entirely. The two remaining variable lines — social data and
manual labor — are the ones with the least information behind them today.

Track manual-labor hours per client per cycle as a first-class cost line, alongside
the existing `llm_cost_calculator.py` totals, so the pass-through invoice is
defensible without reconstruction.

---

## 9. Risks and open questions

### Open questions for the customer
1. **What are the 50 websites?** If they are German e-commerce/review sites
   (Amazon, Idealo, Trustpilot, Otto) rather than media outlets, this is a *product
   review monitoring* problem — different extraction, different report structure,
   arguably more valuable to a consumer-goods supplier. **Answer this before
   writing code.**
2. **Which social platforms specifically**, and what does coverage mean per
   platform — posts only, or comments too? Keyword search, or named-account
   monitoring? Name them in the SOW; "all social media" is a renewal-time trap.
3. Full brand / product / entity list (feeds §5.3).
4. Competitor list, if competitor monitoring is in scope.

### Risks
| Risk | Mitigation |
|---|---|
| **Recall proof** — "how do you know you didn't miss something?" | Two independent discovery paths (site crawl + brand-term search); documented source list; per-source health checks alerting when a source yields zero across two cycles. `health_check.py` is the seed. |
| **Legal / GDPR** — storing named individuals' posts; platform ToS | Licensed providers where possible; minimal PII; pseudonymize author handles unless the account is a media outlet or public figure; documented deletion path. Put it in writing so it cannot come back later. |
| **Manual-labor quality variance** | QC pass in the `manual` connector (§5.1); structured task lists so workers aren't improvising; spot-check against automated results where the two overlap. |
| **Scope creep to real-time** | Hold the biweekly line on the base tier; §7 alert tier is the pressure valve. |
| **Bus factor** (one-man operation) | Answer with ops maturity — monitoring, health checks, heartbeat, automated runs — not headcount. |
| **Home IP blocked, no rotation possible** (D8) | Keep per-domain locks and politeness delays in `parallel_crawler.py` conservative — do not tune concurrency up. Proxy path stays available as fallback. |
| **ISP terms** — consumer contracts typically bar commercial use / servers | Outbound crawling at this volume is indistinguishable from browsing; Tailscale means no inbound service is ever run. Real but negligible. |
| **Home infra reliability** — power cut, ISP outage, forced reboot | Watermark pattern is self-healing on next run; VPS heartbeat catches sustained outages; rolling DB snapshots mean the laptop is disposable. |

---

## 10. Next steps

1. Get answers to §9 Q1 and Q2 — these gate the build.
2. Collect the 50-site list and the entity list.
3. **Run a one-cycle pilot** on the customer's real brands, hand-tuned, delivered as
   a sample report. A weekend of work that triples as sales asset, requirements
   document, and recall test. It will immediately show which of the 50 sites are
   worth crawling and which platforms actually carry their mentions.
4. Scaffold the new repo, vendor the crawler per §4.
5. Stand up the laptop: Tailscale, auto-login, power settings, Task Scheduler,
   VPS backup + heartbeat. Confirm whether the line is DS-Lite/CGNAT (§5.5).
