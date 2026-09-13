# brandmonitor — Project Notes

Monitoring for Chinese consumer brands sold in Germany, with Chinese-language
deliverables. The repository is in MVP build-out: the news fetch stack is vendored,
but the application pipeline is not built yet.

## Working documents

- [mvp_plan.md](mvp_plan.md) is the implementation plan for the first customer cycle.
- [todo.md](todo.md) is the active backlog. Completed implementation history does
  not belong there.
- [docs/source_coverage.md](docs/source_coverage.md) records what each configured
  source actually yields, measured rather than assumed.
- [docs/body_collection.md](docs/body_collection.md) is the body-fetch runbook and
  storage/retry contract.
- [docs/selection_and_assessment.md](docs/selection_and_assessment.md) records the
  durable client-selection rationale and planned assessment funnel.
- [new_product_plan.md](new_product_plan.md) and [plan_v2.md](plan_v2.md) are idea
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
`<news:publication_date>` from a news sitemap. Both survive a restamp; `lastmod`
does not. Every stored row records which field supplied its date in
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

Sources discovered by feed carry a real `<pubDate>` and are not exposed to this.

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

The owner explicitly authorizes Claude to SSH into both hosts for read-only
inspection — `git log`/`status`, service and timer status, journal logs, config
files, `--dry-run` and `--test-push`/`--test-email` checks. Changing either host
(pulling, editing `/etc` files, restarting services, touching the database)
still needs confirmation in the conversation.

The laptop is reachable remotely without being on the same LAN or network:
Tailscale SSH (`ssh -l "dell laptop" 100.80.13.120`) and Chrome Remote Desktop are
both set up under the `stroymaker` Google account, so scheduled collection can keep
running — and be checked on — while away from the machine.

The VPS must not run scheduled collection or analysis. Two independently scheduled
hosts would duplicate spend and create divergent databases.

The VPS can also reach the laptop: its `contabo-server` key is in the laptop's
`C:\ProgramData\ssh\administrators_authorized_keys` (added 2026-09-10), giving it
full admin SSH — the same tier as the owner's own personal keys. This is for manual
disaster recovery and checking on the laptop, not for running anything scheduled;
the "VPS must not run scheduled collection" rule above still applies regardless of
reachability.

The live `.env` belongs on the laptop and is never committed. The VPS needs only the
token required for its scheduled heartbeat/backup role. Activating disaster recovery
and copying any additional credentials are manual operations.

### Deployment

`git pull --ff-only` first on the production laptop and then on the health-check VPS.
It does not run the crawler, touch the database, restart services, or copy secrets.
An uncommitted local working tree is allowed but explicitly reported because those
changes cannot be part of the pushed deployment.

### Scheduled work

**Live since 2026-09-12.** `run_daily.bat` runs on the primary laptop under Task
Scheduler as `brandmonitor-daily`, daily at 06:00 Europe/Berlin. Collection is
unattended; the weekly report stack is still run by hand. The batch now ends its
analysis work with the news-only alert gate, which sends at most one combined email
to the internal reviewer directly from the laptop. It needs the SMTP values in the
laptop's `.env`; the VPS does not relay these emails. With `NTFY_TOPIC` also set,
a sent digest is followed by one ntfy push per alert (title, clipped Chinese summary,
an "Open article" button; at most five, then one overflow notice). The email stays the
record: a failed push is logged and never affects the email's sent state.

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

Restoring: stop whatever holds the database open, decompress the `.gz` over
`data/brandmonitor.sqlite3`, and **delete the `-wal` and `-shm` sidecars** — the
database runs in WAL mode, and stale sidecars beside a restored file get replayed on
next open, silently undoing part of the restore.

## Planned repository shape

```text
run.py                 single entry point
config.json            operational settings, read by src/config.py
input/                 source lists, platform lists, and blacklist, shared by all clients
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
two-day Google News sitemap is not declared in `robots.txt`.

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
