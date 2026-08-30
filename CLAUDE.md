# brandmonitor — Project Notes

Monitoring for Chinese consumer brands sold in Germany, with Chinese-language
deliverables. The repository is in MVP build-out: the news fetch stack is vendored,
but the application pipeline is not built yet.

## Working documents

- [mvp_plan.md](mvp_plan.md) is the implementation plan for the first customer cycle.
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

## Hosts and secrets

| Host | Path | Role |
|---|---|---|
| Windows laptop | `c:\apps\brandmonitor` | primary database and scheduled pipeline |
| Contabo VPS | `/var/www/brandmonitor` | heartbeat, backups, and manual disaster recovery |

The VPS must not run scheduled collection or analysis. Two independently scheduled
hosts would duplicate spend and create divergent databases.

The live `.env` belongs on the laptop and is never committed. The VPS needs only the
token required for its scheduled heartbeat/backup role. Activating disaster recovery
and copying any additional credentials are manual operations.

## Planned repository shape

```text
run.py                 single entry point
clients/<slug>/        client config and prompt inputs
migrations/            ordered SQLite migrations
src/                    application code and prompts
vendor/newscrawler/    existing news discovery/fetch code
vendor/govcrawler/     government fetch code, when added
data/                  gitignored database, cache, PDFs, and reports
tests/
```

Prefer a flat `src/` until several files of the same kind justify a folder.

## Vendored code

Code under `vendor/` originated elsewhere but is maintained as part of this project.
Edit it directly when needed; do not add shims or monkey patches merely to preserve
an upstream diff. Record origins and material local changes in
[vendor/PROVENANCE.md](vendor/PROVENANCE.md).

The existing news crawler imports `src.config`, `src.logger`, and
`src.crawler_news.*`. Integration should provide `src/config.py` and `src/logger.py`;
rewrite the stale `src.crawler_news.*` imports when the vendor tree is first wired
into the application.

## Reference repository

`c:\apps\NewsCrawler` is the local working copy of
`github.com/jinchiluis/germany_risk_monitor`, despite its directory name. It is a
source of fetch, agent, and report mechanics only; brandmonitor must not import it at
runtime.

## Deployment

Deployment is deliberate and pull-based. Use `git pull --ff-only`; scheduled runs do
not update their own code.
