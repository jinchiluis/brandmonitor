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

Collection runs **daily**; customer reports go out **weekly**. Those two cadences
are the design target — a source is worth keeping if it produces something a weekly
report would carry.

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
`published_at_source` (`page`, `feed`, `news_sitemap`, `lastmod`, `frontpage`, or
null); a report may print the first three and must never print `lastmod`. Visible
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

## Hosts and secrets

| Host | Path | Role |
|---|---|---|
| Windows laptop | `c:\apps\brandmonitor` | primary database and scheduled pipeline |
| Contabo VPS | `/var/www/brandmonitor` | heartbeat, backups, and manual disaster recovery |

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

### Scheduled work

**Nothing is scheduled yet, deliberately.** The project is still in the testing
phase and `run_daily.bat` is run by hand. No Task Scheduler entry exists; do not add
one as a side effect of other work.

When the pipeline does go live on the primary laptop, register it under the
logged-on user rather than SYSTEM — SYSTEM sees neither the `.venv` nor the user's
OneDrive folder. The settings that matter are laptop-specific and are not the
defaults: `StartWhenAvailable` catches up a run missed while the machine was off,
and Task Scheduler otherwise refuses to start on battery and stops a running task
when the machine unplugs.

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

`run_daily.bat` ends with a `backup` stage, so the snapshot always carries the day's
collection instead of yesterday's. Backups are `python run.py backup`: an online
SQLite snapshot (safe while the pipeline holds the database open), VACUUMed, gzipped,
rotated into daily/weekly/monthly tiers, then copied to
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
vendor/newscrawler/    existing news discovery/fetch code
vendor/govcrawler/     government fetch code, when added
data/                  gitignored database, backups, cache, logs, PDFs, and reports
tests/
```

Prefer a flat `src/` until several files of the same kind justify a folder.

`input/` holds collection inputs and `clients/<slug>/` holds analysis inputs, which
is the repository-level form of the first design rule. A source list is not client
config: several clients read the same one.

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
