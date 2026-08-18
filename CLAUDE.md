# brandmonitor — Claude Notes

Brand and reputation monitoring for Chinese consumer brands sold in Germany.
Biweekly Chinese-language report over news, trade press, and social media.

**Status: pre-build.** No application code exists yet. This file describes how the
system is *distributed* — hosts, repo layout, deploy, secrets. It deliberately
contains no business logic.

## Where the design lives

[new_product_plan.md](new_product_plan.md) holds scope, architecture, reuse map, cost
model, and open questions. **It is a working document, not a spec** — §9 has customer
questions still unanswered that materially change the build. Do not implement from it
as if it were settled. As decisions harden into code, the durable ones move here.

## Hosts

| Host | Path | Role |
|---|---|---|
| Windows laptop (primary) | `c:\apps\brandmonitor` | full pipeline, SQLite primary, development |
| Contabo VPS (standby) | `/var/www/brandmonitor` | heartbeat watcher, backup target, DR runner |

| | Laptop | VPS |
|---|---|---|
| **Scheduled** | full pipeline | heartbeat watcher, backup retention |
| **On demand** | any stage | DR pipeline run |
| **Never** | — | **scheduled pipeline run** |

The VPS must never run a cycle on a schedule. Two hosts writing one DB means duplicate
LLM spend and a diverged store, and it fails silently. Planned enforcement: `run.py`
refuses a cycle unless an env var marks the host primary; the VPS clone never sets it.

Why the laptop is primary: its residential IP removes the reason the vendored proxy
stack exists. See §5.5 of the plan.

## VPS details

- Host: `144.91.109.185` (`ssh root@144.91.109.185`) — shared with other projects
- App dir: `/var/www/brandmonitor/`, venv `env/` (Python 3.12.3) — use `env/bin/python3`
- Deps not installed yet; there is no `requirements.txt`
- No systemd unit, no cron, no timer. Nothing here runs automatically yet.
- Do not confuse with `/var/www/brandchecker` — unrelated project

## Deploy

Pull, not push. **No CI/CD**, deliberately: GitHub runners cannot reach the laptop
behind CGNAT, and auto-deploying the standby would mean an SSH key in GitHub secrets.

```bash
git pull --ff-only          # never a bare pull — a stray local edit opens a merge
```

Planned around it: reinstall deps when `requirements.txt` moves, and apply pending
migrations at `run.py` startup so a pull can never leave the schema behind the code.
The scheduled run will **not** pull — deploying is a deliberate act at the keyboard.

## Secrets

`.env` at the repo root on both hosts, gitignored, never deployed. VPS copy is
`chmod 600`, root-only. 25 keys: Doubao, BrightData zones + API keys, five paywall
site logins, `SCRAPE_SERVER_TOKEN` (the VPS `/log` sink the heartbeat will reuse), and
empty placeholders for Anthropic, YouTube, and Reddit.

Credentials were copied 1:1 from rewriter's VPS `.env`, so **both products currently
share live production values** — rotating in one place means rotating in both, and the
paywall logins are single-session accounts that can invalidate each other if both
products run concurrently.

The two copies drift silently. Any key added on the laptop must be re-copied to the
VPS by hand until the backup job carries `.env` alongside the DB snapshot.

## Repo layout

Flat by design. `germany_risk_monitor` runs 10k lines on four subfolders; a folder
holding two files is an extra path segment, not organization. Split into a sibling
file first; create a folder only at ~5 files of the same shape.

```
run.py                 # planned — single entrypoint, stage flags
config.json            # planned — workers, models
migrations/            # NNN_*.sql — the schema lives here, nowhere else
clients/<slug>/        # planned — client.json, entities.json, sources.json
src/
  connectors/          # only folder that earns one — ~8 files, one shape
  prompts/             # prompt text files, not inline strings
vendor/newscrawler/    # exists — see below
data/                  # gitignored — SQLite DB, cached html, reports
tests/
```

Only `vendor/`, `migrations/`, the plan, and this file exist so far. Git does not track
empty directories, so a fresh clone looks sparser than the laptop working copy — that
is expected, not a broken checkout.

## Vendored code

`vendor/newscrawler/` is copied **1:1 from germany_risk_monitor** `src/crawler_news/`
at `6a86115` (`origin/master`) — discovery, fetch, extract and paywall in one tree.
[vendor/PROVENANCE.md](vendor/PROVENANCE.md) records the exact commit, what was left
out and why, host integration requirements, and pending ports. Update its "modified
since copy" line on first local edit.

**The vendored code is not self-contained.** It imports `src.config`, `src.logger`,
and `src.crawler_news.*` from the surrounding app. Those names must be satisfied or
the imports patched when `src/` is built — a real decision, not a detail, since our
planned layout calls it `src/log.py`, not `src/logger.py`.

Heavily patched fork of NewsCrawler. Treat it as our code — do not restructure it to
track upstream. BrightData paths stay available but unused for site fetching; on a
residential IP there is nothing to rotate to.

## Source repos (same machine, not dependencies)

- `c:\apps\germany_risk_monitor` — the single source repo. Vendored crawler above,
  plus the planned agent/ops plumbing: assessment, embedding, LLM client and cost
  accounting, logger, watermark pattern, docx report mechanics.
- `c:\apps\rewriter` — **not** a source repo. Its fetch fork leads GRM only in
  BrightData proxy machinery, which D8 makes unnecessary. Two small snippets remain
  worth porting; see PROVENANCE.md.

Reference material to copy from deliberately, not to import at runtime.

## Git

- Repo: `https://github.com/jinchiluis/brandmonitor`, default branch `main`
- Laptop remote is HTTPS (Git Credential Manager); VPS remote is SSH
