# VPS health checker

There are two deliberately separate halves:

- On the laptop, `canary.py` reconciles a few critical publisher endpoints with
  stored URLs and `analyze.py` reads the database's existing run accounting. Both
  are observers: they never write SQLite or change collection decisions.
- On the Contabo VPS, `check.py` reads the laptop's structured
  `data/last_run.json` and `data/health/latest.json` over Tailscale SSH. It never
  runs collection, reads SQLite, or classifies free-form logs.

`run_daily.bat` runs the two laptop observers after the backup, while the existing
run lock still holds. They therefore see the completed daily database, cannot
delay its backup, and cannot race a manual collection. A publisher finding is
written into JSON and exits zero; a nonzero observer stage means the observer
itself failed.

## Laptop observations

`health/canary.py` currently checks four deliberately small independent routes:
etailment's news sitemap, Onlinehändler-News' newest article-sitemap page,
LOGISTIK HEUTE's RSS feed, and DVZ's rolling news sitemap. It validates the XML,
rejects common access-challenge pages, and compares the ten newest eligible URLs
with `raw_item`. Configuration lives in `health/canaries.json`; Bright Data and
automatic remediation are not part of this path.

`health/analyze.py` reads `run`, `run_source`, per-source collection watermarks and
the current `body_fetch` queue. It detects missing source accounting, individual
source failures hidden by an otherwise successful command, checkpoint mismatches,
repeated body failures, consecutive zeros, and sustained yield collapse. Volume
rules stay in `learning` until seven comparable prior daily runs exist; initial
backfills and recovery windows longer than 36 hours do not train the baseline.

Both observers retain the first immutable observation for each news run and
atomically replace a latest pointer:

```text
data/health/canaries/latest.json
data/health/canaries/history/<cycle>-news-<run>.json
data/health/latest.json
data/health/history/<cycle>-news-<run>.json
```

The database remains the durable history for collection counts. These snapshots
preserve derived verdicts and external canary evidence. `rules_version`, source
configuration hashes and run ids make later rule changes visible.

Run either observer manually without changing the database:

```powershell
.venv\Scripts\python health\canary.py --cycle-date 2026-09-13
.venv\Scripts\python health\analyze.py --cycle-date 2026-09-13
```

`health/sampling_prompt.md` is the separate weekly adversarial review runbook. It
has an agent produce a reproducible mixture of random drops, risk-weighted drops
and kept controls under `data/health/sampling/`; it never authorizes automatic
profile or code changes.

## VPS evaluation

The check order is:

1. A marker older than 26 hours is an alert, even if its exits were zero.
2. A fresh marker with any non-zero stage is an alert.
3. A missing, malformed, stale or older-cycle coverage snapshot is an alert.
4. A coverage `warning` or `critical` is an alert; `learning` and `healthy` are
   non-alerting.

If SSH temporarily fails, a cached fresh marker or quality snapshot provides the
remainder of its 26-hour grace period. With no fresh cache, SSH failure alerts.
State in `/var/lib/brandmonitor-health/state.json` ensures one email per stable
incident set and one email when service recovers. A newly appearing source incident
changes the incident key and sends an updated alert. Failed email attempts are not
latched, so the next timer run retries them.

SMTP stays outside this repository. The service reads `SMTP_PASSWORD` from the
existing `/root/cost_dashboard/.env`; the token is never copied into Brand
Monitor. Defaults match Cost Dashboard's current sender and recipient. They can
be overridden with `--sender` and `--recipient` or `EMAIL_SENDER` and
`EMAIL_RECIPIENT`.

## Verify on the VPS

Run a probe without sending mail or changing state:

```bash
cd /var/www/brandmonitor
python3 health/check.py --dry-run
```

For local development, point it at both files:

```powershell
.venv\Scripts\python health\check.py --dry-run `
  --marker-file data\last_run.json --quality-file data\health\latest.json
```

Send one real SMTP test:

```bash
python3 health/check.py --test-email --env-file /root/cost_dashboard/.env
```

Do not enable the timer until the Windows daily task is live; otherwise the
absence of a marker is correctly reported as an incident.

## Install the timer

After the repository has been pulled to `/var/www/brandmonitor`:

```bash
install -m 0644 health/brandmonitor-health.service /etc/systemd/system/
install -m 0644 health/brandmonitor-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now brandmonitor-health.timer
systemctl list-timers brandmonitor-health.timer
```

Inspect recent checks with:

```bash
journalctl -u brandmonitor-health.service --since today
```

The VPS root account must retain its existing SSH key and known-host entry for
`ssh -l "dell laptop" 100.80.13.120`.
