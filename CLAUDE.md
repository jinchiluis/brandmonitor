# brandmonitor — Project Notes

Monitoring for Chinese consumer brands sold in Germany, with Chinese-language
deliverables. Collection, gating, internal alerts and the weekly report stack are
built. Collection is scheduled on the primary laptop; launch validation and the
remaining reliability work are tracked in `crawl_tasks.md`.

## Working documents

- [mvp_plan.md](docs/Designs/mvp_plan.md) is the implementation plan for the first customer cycle.
- [crawl_tasks.md](crawl_tasks.md) is the remaining crawl launch work and monitoring
  plan; [weaknesses.md](weaknesses.md) records its evidence and unresolved findings.
- [todo.md](todo.md) is the active backlog. Completed implementation history does
  not belong there.
- [docs/source_coverage.md](docs/source_coverage.md) records what each configured
  source actually yields, measured rather than assumed.
- [docs/body_collection.md](docs/body_collection.md) is the body-fetch runbook and
  storage/retry contract.
- [docs/selection_and_assessment.md](docs/selection_and_assessment.md) records the
  durable client-selection rationale and planned assessment funnel.
- [new_product_plan.md](docs/Designs/new_product_plan.md) and
  [plan_v2.md](docs/Designs/plan_v2.md) are idea
  archives. They contain useful research and possible later features, but they are
  not build specifications.

When the documents disagree, follow `mvp_plan.md`.

## Design rules

- Collection is source-specific; analysis is client-specific.
- Store normalized source material before applying a client relevance prompt.
- A new customer should normally require config and prompts, not another scraper.
- Client assessments must be traceable to the client and prompt/profile version.
- Keep regulatory and reputation processing independent so one can fail or run
  without blocking the other.
- Features outside the MVP stay out until a real cycle demonstrates the need.

## Operating model

Collection and the internal news-alert review run **daily**; customer reports go
out **weekly**. Those cadences are the design target — a source is worth keeping if
it produces something a weekly report or a genuine potential-alert email would
carry.

The 30-day collection window is inherited from `vendor/newscrawler` and is an
emergency backstop for refilling after an outage, not the operating cadence. Do not
read it as "we look back 30 days"; on a daily run almost everything it returns has
been seen before.

## Publication dates are not `lastmod`

A sitemap's `<lastmod>` means "this URL changed". It is publisher-controlled and is
rewritten by CMS migrations, template edits, and nightly regeneration jobs. It is a
**change signal only** — never store it as a publication date, sort on it, or show
it to a customer.

Take the publication date from the page itself: schema.org `datePublished`,
`article:published_time` or a lone `<time datetime>` during body fetch, or
`<news:publication_date>` from a news sitemap. These are publisher assertions of
publication, not guarantees against restamping: the September 15 audit found news,
feed and even page publication fields moving on updates (weaknesses.md W3/W9).
Every stored row records which field supplied its date in
`published_at_source` (`page`, `feed`, `news_sitemap`, `record`, `lastmod`,
`frontpage`, or null); a report may print the first four and must never print
`lastmod`. `record` is the date a structured record gives for its own event — a
DIP procedure step, a European Parliament event. Visible
text is never parsed for a date: the first visible date on a page was a future
seminar on one sampled source and a listing entry on another.

Two sources proved this, and they fail in opposite directions, so a recency check
built on `lastmod` would have caught neither:

| Source | `lastmod` behaviour | Effect |
|---|---|---|
| BVL | all 5,323 URLs share one identical timestamp, regenerated periodically | dates always look fresh; ancient pages pass any recency gate, and every regeneration re-queues every body fetch |
| etailment | whole archive restamped to one day by a migration | dates frozen in the past; genuinely new articles look stale |

etailment's stored rows were 90% pre-2025 archive presented as current. Audit a new
sitemap source for this before trusting its dates: count distinct publication *days*
against row count, and check the lag between the busiest day and the fetch day. A
high share on one day, well before the fetch, is a restamp rather than a busy day.

Feed `<pubDate>` is preferable to sitemap `lastmod`, but some publishers rewrite
it on updates too. Preserve its provenance and keep first-seen time distinct.

The same rule applies to API sources under a different field name. In the DSA
Transparency Database, `received_date` is when a platform *submitted*, not when it
acted — Shein filed 209,921 statements on one day and nothing on most others, and
that single batch carried 221 distinct `application_date` values reaching back to
2024-02-26. Take the event date from `application_date` or `content_date`. Ask of
any new structured source which of its dates is the event and which is the
paperwork, because they are rarely the same field.

DIP, the Bundestag's API, has both traps at once. `aktualisiert` is a re-indexing
stamp — in one 14-day window 1,306 of 1,858 touched current-term records were
written questions a median 340 days old — so it sets the collection window and
nothing else. `datum` is a procedure's latest step, not its adoption: a Bundesrat
resolution adopted 2025-07-11 carries 2026-07-16, the government's reply. A report
names the step, not only the date.

## Hosts and secrets

| Host | Address | Path | Role |
|---|---|---|---|
| Windows laptop | Tailscale `100.80.13.120` (`ssh -l "dell laptop"`) | `c:\apps\brandmonitor` | primary database and scheduled pipeline |
| Contabo VPS | Tailscale `100.120.172.43`, public `144.91.109.185` (`root`) | `/var/www/brandmonitor` | heartbeat, backups, and manual disaster recovery |

The owner explicitly authorizes Claude to SSH into both hosts — inspection
(`git log`/`status`, service and timer status, journal logs, config files,
`--dry-run`, `--test-push`/`--test-email`) and the operational changes a task
calls for (editing host config such as `/etc/brandmonitor-health.env`, pulling,
restarting services). Deleting or overwriting data, backups, or the database
still needs confirmation.

The laptop is reachable remotely without being on the same LAN or network:
Windows OpenSSH over Tailscale (`ssh -l "dell laptop" 100.80.13.120`) and Chrome
Remote Desktop are both set up under the `stroymaker` Google account, so scheduled
collection can keep running — and be checked on — while away from the machine.
Tailscale key expiry is disabled on the laptop and the VPS, sleep is off on AC and
battery, auto logon is on, and Windows Update active hours are 05:00–23:00. The
setup, its reasons and an audit script are in the Obsidian note
`REMOTE-ACCESS-PLAYBOOK.md`.

The VPS must not run scheduled collection or analysis. Two independently scheduled
hosts would duplicate spend and create divergent databases.

The VPS can also reach the laptop: its `contabo-server` key is in the laptop's
`C:\ProgramData\ssh\administrators_authorized_keys` (added 2026-09-10), giving it
full admin SSH — the same tier as the owner's own personal keys. This is for manual
disaster recovery and checking on the laptop, not for running anything scheduled;
the "VPS must not run scheduled collection" rule above still applies regardless of
reachability.

A development checkout has no database, logs or run markers. Inspect production with
`tools/laptop.py` rather than hand-built SSH lines: the laptop's SSH shell is German
Windows PowerShell, so `python -c "..."` and cmd-style `set X=... &` fail to parse and
relative `data/` paths resolve against the SSH user's home. The tool pipes everything
over stdin, and runs locally when it finds the database beside it.

```text
python tools/laptop.py status                  # deployed commit, tasks, run markers, lock
python tools/laptop.py sql "SELECT ..."        # read-only; --json for untruncated rows
python tools/laptop.py py path/to/script.py    # runs in the laptop repo with its venv
python tools/laptop.py ps "Get-ScheduledTaskInfo -TaskName brandmonitor-daily"
python tools/laptop.py admin                   # read-only monitor at http://127.0.0.1:8765/
```

The live `.env` belongs on the laptop and is never committed. The VPS needs only the
token required for its scheduled heartbeat/backup role. Activating disaster recovery
and copying any additional credentials are manual operations.

### Deployment

`git pull --ff-only` first on the production laptop and then on the health-check VPS.
It does not run the crawler, touch the database, restart services, or copy secrets.
An uncommitted local working tree is allowed but explicitly reported because those
changes cannot be part of the pushed deployment.

### Scheduled work

**As of 2026-09-16** the daily task is enabled, the intraday task is disabled and
the canaries are off. Check actual task settings with `python tools/laptop.py
status` before relying on a documented capability.

**Live since 2026-09-12.** `run_daily.bat` runs on the primary laptop under Task
Scheduler as `brandmonitor-daily`, daily at 06:00 Europe/Berlin. Collection is
unattended; the weekly report stack is still run by hand. The batch now ends its
analysis work with the news-only alert gate, which sends at most one combined email
to the internal reviewer directly from the laptop. It needs the SMTP values in the
laptop's `.env`; the VPS does not relay these emails. With `NTFY_TOPIC` also set,
a sent digest is followed by one ntfy push per alert (title prefixed with the client
name, clipped Chinese summary, published date or "unknown", source slug, and the
article URL as a bare line so ntfy auto-linkifies it — no action button; at most
five, then one overflow notice). Each email entry is likewise prefixed with the
client name and carries the same published date. The email stays the record: a
failed push is logged and never affects the email's sent state.

It is registered under the logged-on user rather than SYSTEM — SYSTEM sees neither
the `.venv` nor the user's OneDrive folder. The settings that matter are
laptop-specific and are not the defaults: `StartWhenAvailable` catches up a run
missed while the machine was off, and Task Scheduler otherwise refuses to start on
battery and stops a running task when the machine unplugs.

```powershell
$a = New-ScheduledTaskAction -Execute "C:\apps\brandmonitor\run_daily.bat" `
       -WorkingDirectory "C:\apps\brandmonitor"
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
       -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
       -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "brandmonitor-daily" -Action $a -Settings $s `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 6am)
# check: Get-ScheduledTaskInfo -TaskName "brandmonitor-daily"  (LastTaskResult 0 = ok)
```

An `Interactive` principal runs only while that user is logged on, which is why the
remote access above is part of the operating arrangement rather than a convenience.
Running whether-logged-on-or-not requires storing a password.

**Intraday news pass (optional; paused as of 2026-09-16).** When enabled,
`run_intraday.bat` repeats only the news path —
collection, title gate, body fetch, news body gate, alert gate — every two hours
from 08:00 to 22:00, so a potential alert reaches the reviewer the same day rather
than after the next 06:00 run. 23:00–05:00 stays free for Windows updates and
restarts. The 06:00 daily run remains the complete record and the only one that
collects regulatory sources, backs up and runs the health observers; collection
windows chain from each source's watermark, so it simply continues where the last
intraday pass stopped. The two share `data/run.lock`: a slot that finds the lock
held exits 3 and writes no marker. Its own marker is `data/last_intraday_run.json`,
which `health/check.py` alerts on only when it failed after the latest daily run.

Discovery checkpoints chain, with a 48-hour overlap in sitemap/feed filtering.
Every stage but one has a resume mechanism:
collection and the alert gate from watermarks, body fetch and body gate from their
queues. The title gate judges only the latest news run, so a gate that exited 2 or
crashed leaves that run unjudged for good. Transient API errors are not this case —
those batches are kept whole. Fix the cause, then run
`tools\repair_title_gate.bat` (no arguments lists recent news runs) with the
affected run ids: it re-gates them under the run lock, fetches the kept bodies and
runs the alert gate. This is deliberately manual; the failure is rare and its cause
needs a person anyway.

```powershell
$a = New-ScheduledTaskAction -Execute "C:\apps\brandmonitor\run_intraday.bat" `
       -WorkingDirectory "C:\apps\brandmonitor"
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -MultipleInstances IgnoreNew
$t = New-ScheduledTaskTrigger -Daily -At 8am
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 8am `
       -RepetitionInterval (New-TimeSpan -Hours 2) `
       -RepetitionDuration (New-TimeSpan -Hours 14 -Minutes 30)).Repetition
Register-ScheduledTask -TaskName "brandmonitor-intraday" -Action $a -Settings $s -Trigger $t
```

It deliberately omits `StartWhenAvailable`: a missed slot is covered by the next
one, and catching one up after a night-time restart would run inside the update
window.

**No internet means no run.** Both batch files call `run.py netcheck` before
anything else and exit **4** without attempting a stage when this host is offline,
writing a marker whose only stage is `netcheck`. `health/check.py` reads that back
as `offline` and says so, instead of reporting the stage codes each collector
happens to produce when its first request fails — a city outage on 2026-09-16
arrived as `title_bodies=1, safety_gate=2, dip=1, ep_procedures=2`, which is four
descriptions of one cause. `src/net.py` probes resolution *and* routing, because
they fail independently and DNS-only failure is the mode a single ping calls
healthy.

There is deliberately no wait-and-retry loop. With intraday enabled, news has nine
scheduled passes a day; during its current pause the next automatic attempt is
the next daily run. Watermarks and queues retain unfinished work, but publisher
feed/frontpage retention and fixed sitemap page ranges limit what can be recovered
(weaknesses.md W21). After an outage, reconcile that coverage and any interrupted
title-gate run. The intraday pass is news-only, so `regulatory`, `safety_gate`,
`dip`, `ep_procedures`, `backup` and the observers wait for the next 06:00 unless
`run_daily.bat` is re-run manually.

An outage must also not retire the body queue. A connection-level failure before
anything in a pass has been fetched is this host's network, not the page, so it
does not spend the URL's `attempts` (`src/bodies.py`); at nine passes a day
against `max_attempts: 5`, an offline day would otherwise retire every pending URL
before lunchtime — silently, since `unavailable` is a legitimate outcome that
fails no stage. Ten such failures in a row across three hosts stop the pass.

**06:00 means Europe/Berlin, and the host has to agree.** The laptop ran on China
Standard Time until 2026-09-12, which would have fired the trigger at midnight CEST
and named `data/log/<date>/` directories by a date rolling over at 18:00 Berlin; the
timezone was corrected to W. Europe. Publication measured across 21,421 dated items
peaks 11:00–16:00 CEST, with 13.2 % of a day published by 06:00 and 91.5 % by 19:00,
so a 06:00 boundary cuts the day in its trough and each run carries one complete
calendar day. `w32time` is enabled and synchronising — a host that stamps every
`fetched_at` must not free-run. Stored timestamps are UTC throughout (`utcnow` in
`src/db.py`), so a timezone error moves the schedule and the log directory names,
never the data.

The VPS half is live too: `health/check.py` runs every 15 minutes under
`brandmonitor-health.timer`, reads `data/last_run.json` and
`data/health/latest.json` from the laptop over Tailscale SSH, and emails when the
marker is older than 26 hours, any stage exited non-zero, or the coverage observer
reports a warning/critical condition. It pushes to its own developer topic,
`NTFY_HEALTH_TOPIC`, never the admins' alert `NTFY_TOPIC`. It never opens the database — see
[health/README.md](health/README.md).

**Operational monitoring tables** (`src/monitoring.py`, migration 007) record what
the pipeline did so problems can be seen per pass, source and file rather than
reconstructed from logs: `pipeline_pass` (every batch invocation, including offline
and lock-skipped slots, with deployed commit and config hashes, written by
`run.py record-pass` at each batch exit), `discovery_source` (per source per
collection run: attempts versus responses, bytes, 304s, window, watermark before and
after, drop counts), `fetch_event` (every discovery request, answered or not, with
what each sitemap/feed file held and its newest/oldest entry date) and
`body_attempt`. The contract is one-way: rows are written after the production
commit in their own transaction, failures only log, and nothing in the pipeline
reads them. Keep it that way — a monitoring change must never alter an outcome.
Stage runs belong to the pass whose time span contains them. `PoliteAdapter.requests`
counts responses only, so a request that got none exists only in `fetch_event`: on
2026-09-16 at 06:00, 23 of 24 news sources logged `0 request(s)` and were stored
as `zero`. The daily pass prunes these four tables to a rolling
`monitoring.retention_days` (30) window; runs, `run_source` and the corpus are never
pruned by it. Frontpage fetches that fall back to a headless browser bypass the
adapter and are not recorded.

`tools/admin.py` is the read-only monitor over all of this plus runs, the body queue,
gate decisions, the title-gate JSONL, markers, the health verdict and logs: schedule
strip, source × run heatmap (quiet `zero` versus unanswered `no response`), per-file
request history, run detail, bodies, funnel, log viewer. Standard library only, no
import from `src/`, database opened `mode=ro` + `query_only`, GET only, bound to
127.0.0.1. Passes before migration 007 are derived from stage runs and marked so.

On the laptop it is always on: the `brandmonitor-admin` task (S4U, so it runs
without a logged-on user and without a console window) starts it at boot and
retries every 10 minutes, and `tailscale serve --bg 8765` publishes that loopback
port to the tailnet only, over HTTPS, at
`https://desktop-paf96vp.tail33e56b.ts.net/`. The server still binds 127.0.0.1 and
has no login, so never use `tailscale funnel` for it. Output goes to
`data/log/admin.log`. `python tools/laptop.py admin` still works from any checkout:
it forwards the port over SSH and uses the running server when there is one.

```powershell
$root = 'C:\apps\brandmonitor'
$a = New-ScheduledTaskAction -Execute 'cmd.exe' -WorkingDirectory $root `
       -Argument "/c `"$root\.venv\Scripts\python.exe tools\admin.py >> data\log\admin.log 2>&1`""
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$again = New-ScheduledTaskTrigger -Daily -At 12am
$again.Repetition = (New-ScheduledTaskTrigger -Once -At 12am `
       -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$p = New-ScheduledTaskPrincipal -UserId 'DELL Laptop' -LogonType S4U
Register-ScheduledTask -TaskName 'brandmonitor-admin' -Action $a -Settings $s -Principal $p `
  -Trigger (New-ScheduledTaskTrigger -AtStartup), $again
tailscale serve --bg 8765        # once; persists across reboots. Undo: tailscale serve reset
```

`run_daily.bat` makes `backup` the last stage that touches the corpus, so the
snapshot always carries the day's collection instead of yesterday's. Two read-only
health observers run after it while the lock remains held, publishing JSON outside
SQLite. Backups are `python run.py backup`: an online SQLite snapshot (safe while
the pipeline holds the database open), VACUUMed, gzipped, rotated into
daily/weekly/monthly tiers, then copied to
`~/OneDrive/brandmonitor-backups` — a plain directory the OneDrive client already
syncs, so there is no `rclone` remote and no OAuth token on this host. Retention and
paths live in `config.json`. The backup command is useful on its own during testing;
it does not need a scheduler.

Each backup also writes `brandmonitor-state-<date>.tar.gz` beside the snapshot:
`data/reports/` and `data/title_gate/` (`backup.state_dirs`), which the database does
not hold — the issue registers, the frozen bundles and the title-gate keeps. It
rotates and goes off-box with the snapshot; a failure there never costs the
snapshot. Logs, health JSON and run markers are regenerated and stay out.

Restoring: stop whatever holds the database open, decompress the `.gz` over
`data/brandmonitor.sqlite3`, and **delete the `-wal` and `-shm` sidecars** — the
database runs in WAL mode, and stale sidecars beside a restored file get replayed on
next open, silently undoing part of the restore. Then extract the same date's state
archive into `data/` (`tar -xzf brandmonitor-state-<date>.tar.gz -C data`).

## Planned repository shape

```text
run.py                 single entry point
config.json            operational settings, read by src/config.py
input/                 source lists and platform lists, shared by all clients
clients/<slug>/        client config and prompt inputs
migrations/            ordered SQLite migrations
src/                    application code and prompts
src/report_agent/      weekly assessment and report stack
vendor/newscrawler/    existing news discovery/fetch code
vendor/govcrawler/     government fetch code, when added
data/                  gitignored database, backups, cache, logs, PDFs, and reports
tests/
```

Prefer a flat `src/` until several files of the same kind justify a folder.

`input/` holds collection inputs and `clients/<slug>/` holds analysis inputs, which
is the repository-level form of the first design rule. A source list is not client
config: several clients read the same one.

## The weekly report stack

`src/report_agent/` turns a week of stored material into a Chinese customer report.
Four commands, and only the second calls a model:

```text
python run.py export-window --client jt-express --since 2026-09-05 --until 2026-09-11
python run.py assess        --bundle data/reports/jt-express-2026-09-05_2026-09-11
python run.py report        --bundle data/reports/jt-express-2026-09-05_2026-09-11
python run.py verify-report --bundle data/reports/jt-express-2026-09-05_2026-09-11
```

`export-window` freezes a bundle under `data/reports/<client>-<since>_<until>/`
through a `mode=ro` URI inside a rolled-back transaction, so it is safe while
collection runs. The frozen half is written once and no later stage modifies it;
`assess`, `report` and `verify-report` are re-runnable against the same bundle for
as long as it exists. `assess` makes no network call except to the model, and no
stage after `export-window` reads the database.

Four properties hold the whole thing together, and breaking any of them is a
regression rather than a style choice:

- **Relevance is not treatment.** The gates decide whether an item is a signal;
  the report decides what happens to it — `report`, `merge`, `background_only`,
  `carry_forward`, `insufficient_evidence`, `omit_for_priority`. `merge` and
  `carry_forward` are structurally impossible for a per-item scorer, which is why
  the assessment is weekly and clustered rather than per item.
- **Complete accounting.** Every identity in the export carries exactly one
  ledger row. A report claiming eight findings without saying what happened to
  the other 996 cannot be checked, so `report` fails on a gap rather than
  rendering a partial ledger.
- **Links resolve by id.** The writing step emits `[文字](item:24617)` and the
  renderer substitutes the URL the export froze. An id outside the export fails
  the build, which is what makes a fabricated source impossible rather than
  something a reviewer has to notice.
- **Depth is measured, not asserted.** `review_depth` in the ledger comes from
  the assessor's logged tool calls: `get_body` was called, so that row reads
  `full_stored_body`; nothing was, so it reads `title_only`.

The unit of state between weeks is `issue-register.json`, not last week's prose:
dated status, the evidence behind it, and the evidence that would trigger the next
update. The next cycle's carry-forward step searches for exactly that, **including
among the items a gate stopped** — a continuing story often fails a per-item
relevance test, and recovering it is the reason stopped items stay in the bundle.

The report stack is deliberately **not** in `run_daily.bat`. Collection is daily
because it is irreversible; assessment reads only the database and is completely
reversible, so it runs weekly and by hand for now (todo.md §1.1).

## Vendored code

Code under `vendor/` originated elsewhere but is maintained as part of this project.
Edit it directly when needed; do not add shims or monkey patches merely to preserve
an upstream diff. Record origins and material local changes in
[vendor/PROVENANCE.md](vendor/PROVENANCE.md).

The news crawler is wired in: its modules sit directly under `vendor/newscrawler/`
and import as `vendor.newscrawler.<module>`. It takes only `CRAWLER_VERBOSE` from
`src/config.py` and `get_logger` from `src/logger.py`. Keep that surface small — a
vendored module reaching further into `src/` is a sign the boundary is slipping.

## Adding news sources

Sites to crawl live in a JSON array read by
[source_loader.py](vendor/newscrawler/source_loader.py). An entry needs `url`;
`sitemap`, `feeds`, `frontpage`, and `brightdata` switch on discovery methods.
`organization` names the site for CLI filters. Remaining keys are metadata the
crawler ignores.

An entry with `collector` is not crawled: it names the collector that stores its
items (`dip` for `run.py collect-dip`, `ep_procedures` for `collect-ep`). It stays in
the list because the selector and the body gates find a source's items through it.
`"keyword_prefilter": false` makes every item of a source a candidate — for a small
source like the EP procedures, where the keyword rules save nothing and would lose
items whose titles carry none, such as "Clean corporate vehicles".

`feed_urls` is an optional list of exact feed URLs. When set, it replaces both
homepage autodiscovery and `COMMON_FEED_PATHS` for that source. Use it whenever a
publisher's feed is unconventional, and whenever a site advertises several feeds and
only one is wanted — autodiscovery takes what it finds first, which on
bundesnetzagentur.de is energy auctions rather than press releases.

`extra_sitemap_urls` is an optional additive list of sitemap roots. They are merged
with anything declared in `robots.txt`; unlike guessed paths, they are used even
when robots already declares a different sitemap. Use this for a verified omission,
not to turn common-path guessing on for every source. DVZ needs it because its
two-day Google News sitemap is not declared in `robots.txt`. A configured root also
switches the guesses off, which is why faz.net (no robots sitemap) lists its two.

`sitemap_urls` **pins** discovery rather than supplementing it: an exact list of
the files that carry a source's articles, used in place of `robots.txt`, the
guessed paths, the index walk and `extra_sitemap_urls`. Measured over a live
50-hour window on 2026-09-16, faz.net answered 100 sitemap files of which 95 held
nothing in the window, and dvz.de 88 of which 87 did; neither host sends `ETag` or
`Last-Modified`, so conditional requests cannot help them either. Pin what the
probe shows carries articles, never what the site looks like it should have.

A pinned URL may carry `{YYYY}`, `{MM}` or `{DD}`, resolved against the collection
window — so a run inside the 48-hour overlap at a month boundary reads both months.

Paged sitemaps need one of two tokens, and **which one depends on which end of the
numbering is new — ask before pinning**. `{LATEST}` reads the named index and takes
the highest-numbered child, which is right for ohn.haendlerbund (page 34) and
Wettbewerbszentrale (`post-sitemap4.xml`). `{PAGE}` names a fixed range directly and
reads no index, which is right for spiegel.de, where page 1 holds the newest 50
articles and page 30 the start of the month — pinned with `{LATEST}` it fetched four
empty files and re-read a 23,635-child index every pass.

```json
{"url": ".../sitemap.xml?page={LATEST}", "index": ".../sitemap.xml", "latest_count": 1}
{"url": ".../sitemap-{YYYY}-{MM}_{PAGE}.xml", "pages": [1, 6]}
```

`{LATEST}` matches on what precedes the number and takes the rest of the URL from
the index, because a TYPO3 paged sitemap gives every page its own `cHash` that no
template can predict. A `{PAGE}` range is one group, so pages that do not exist yet
on the 1st of a month are logged rather than fatal.

A pinned file that **cannot be read** fails the source and holds its watermark: a
404 or 5xx, a redirect to another host, markup where XML was promised, XML that
will not parse, or a root element that is not `<urlset>`/`<sitemapindex>`. A
pinned file that is **readable and empty** does not — that is a Sunday, and a rule
failing on it would fire about a hundred times a year. In an expanded group,
404/410 is tolerated if a sibling reads; other failures fail the source. That
tolerance currently also excuses required older files, and the recover parser
can accept partial XML; a leaf returning a sitemap index is accepted but its
children are ignored. These open gaps are in weaknesses.md W19.

A file that still parses but has stopped carrying a section needs a coverage
comparison. `tools/rediscover.py` compares a wider traversal using the production
parser; it is not independent and currently omits traversal-cap reporting.
`health/canary.py` has an independent parser but is disabled.
The temporary manual check and conditions for restoring it are in crawl_tasks.md C5.

`origin` skips the homepage probe for a pinned host. Only `frontpage` and feed
autodiscovery start from a homepage, so a source that enables neither — feeds off
or pinned in `feed_urls` — is not probed; its configured host is the origin.

Collection runs nine times a day, so a request that finds nothing is sent nine
times a day. Once the probe has shown which feeds a source really has, write them
into `feed_urls` rather than leaving autodiscovery and `COMMON_FEED_PATHS` to find
them again on every pass. Discovery is polite by construction (`src/polite_http.py`):
sitemap and feed requests are conditional and a 304 replays the stored entries, and
a host answering 429/503 twice stops that source for the pass, which reports it
failed so its window is re-covered.

`allowed_dirs` means something different to each method, which is the sharpest edge
in this config format:

| Method | Effect of `allowed_dirs` |
|---|---|
| `sitemap` | Filters results — URLs outside those prefixes are dropped. |
| `frontpage` | Seeds navigation — those section pages are what gets scraped. Results are not filtered; a link to any section is kept. |
| `feeds` | Ignored entirely. |

So an unset `allowed_dirs` means "keep everything" for sitemaps but "scrape the
homepage and four English-language guesses (`news`, `world`, `business`,
`technology`)" for frontpage — which on a German site finds almost nothing. Set it
whenever `frontpage` is on.

`excluded_dirs` is a final output rule: matching path prefixes are removed from
sitemap, feed, and frontpage results, and are also ignored by body backfills and
candidate selection. Use it for a section that should never enter the corpus.
`excluded_url_patterns` applies the same way but takes regular expressions matched
against the URL path, for index pages that share a prefix with their articles.

Crawl every new site once before writing its entry. Do not infer these values from
how the site looks:

```text
python run.py probe https://www.example.de/          # discovery, before the entry exists
python run.py probe https://www.example.de/ --sources input/germany_medias.json
```

A domain absent from the JSON is probed with all discovery methods and no directory
filter, so the first run reports what the site actually supports and how its article
URLs divide across path prefixes. Set the booleans to the methods that returned
articles, choose `allowed_dirs` from the printed table rather than from the drafted
suggestion, then re-run with `--sources` to confirm the entry behaves as intended.

Read an empty result as "not proven" rather than "unsupported". The probe retries
each method three times because throttling looks exactly like an absent feature, and
a wrongly recorded `false` silently costs coverage for as long as the entry lives.
Where the counts matter, check whether a method stopped at its cap before trusting
the proportions.

For a domain that is listed, an omitted boolean means off rather than on, so a row
carrying only `url` is crawled by nothing. `brightdata` replaces the other methods
instead of supplementing them; reserve it for domains that fail without it. Paywalled
sites also need an entry in
[paywalls.json](vendor/newscrawler/paywall/paywalls.json), a login module, and
credentials in `.env`.

## Reference repository

`c:\apps\NewsCrawler` is the local working copy of
`github.com/jinchiluis/germany_risk_monitor`, despite its directory name. It is a
source of fetch, agent, and report mechanics only; brandmonitor must not import it at
runtime.

## Deployment

Deployment is deliberate and pull-based. Use `git pull --ff-only`; scheduled runs do
not update their own code.
