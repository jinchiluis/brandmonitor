# VPS health checker

`check.py` runs on the Contabo VPS and reads the laptop's structured
`data/last_run.json` over Tailscale SSH. It deliberately does not run collection,
read the SQLite database, or classify free-form logs.

The check order is:

1. A marker older than 26 hours is an alert, even if its exits were zero.
2. A fresh marker with any non-zero stage is an alert.
3. A fresh marker with all-zero stages is healthy.

If SSH temporarily fails, a cached fresh marker provides the remainder of its
26-hour grace period. With no cached marker, or once that marker is stale, SSH
failure alerts. State in `/var/lib/brandmonitor-health/state.json` ensures one
email per incident and one email when service recovers. Failed email attempts are
not latched, so the next timer run retries them.

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
