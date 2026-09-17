# Publisher rejection — cooldown and alarm

Written **2026-09-17**. This is the implementation brief for [crawl_tasks.md](crawl_tasks.md)
C3 / [weaknesses.md](weaknesses.md) W20, narrowed to one mechanism: when a publisher
rejects us, stop asking for a while, and make sure a person finds out.

Scope note: the spoofed browser User-Agent and the absent `robots.txt` handling are
**out of scope here** and are being done in a separate session. Those are about why a
publisher decides to reject us; this file is about what happens afterwards.

## The problem this solves

`run_intraday.bat` runs eight slots a day (08:00–22:00, every two hours), so with the
daily run there are nine passes. Two stages send publisher traffic: `collect --kind news`
and `fetch-bodies`. Neither remembers a rejection past the end of its own pass.

- `PoliteAdapter` (`src/polite_http.py`) does the right thing inside one discovery pass —
  waits out the first 429/503, honours `Retry-After`, trips the source on the second —
  and then discards `tripped`. A publisher that says "back off" at 10:00 is asked again
  at 12:00, 14:00, 16:00 and five more times that day. A `Retry-After: 3600` is respected
  for seconds and ignored by the following seven slots.
- Body fetching has no throttle handling at all. `_fetch_public` (`src/bodies.py:479`) is a
  plain `requests.get` outside that adapter. A 429 reaches `raise_for_status()` and becomes
  an `HTTPError` string in `BodyResult.error`, counted as a normal failed attempt.

The second one is the damaging half. `body_fetch.max_attempts` is 5 and the slots are two
hours apart, so **a host in a temporary block retires its whole queue as `unavailable`
inside one day**. The code already reasons in exactly this shape for connection failures —
see the `untried` guard and `TRANSPORT_BREAKER_*` in `run_body_fetch` — but that guard
covers only transport errors before the first success in a pass. 403 and 429 fall straight
through it. Production has already shown the terminal path firing: one Verbraucherzentrale
item became `unavailable` after five 403s.

## What already exists

The consumption side is built and tested, and nothing below needs to change it.

`--exclude SLUG` is on both `collect` (`run.py:898`) and `fetch-bodies` (`run.py:915`), and
both route through `pause_for_pass` (`src/collect.py:213`), which switches every discovery
method off for that pass rather than dropping the source from the list. That distinction is
the whole reason this is cheap:

- no request of any kind is sent, including the origin probe;
- the **watermark holds**, so the next un-excluded pass re-covers the missed window;
- the **body queue is untouched and no attempt is spent**;
- a `run_source` row is still written as `paused`, so `health/analyze.py` does not report a
  critical `missing_source_result`.

`tests/test_bodies.py:314` asserts precisely this, down to `("failed", 1)` — *"queue kept,
not spent"* — and that the following pass without the flag resumes the source.

`slug_for` (`src/collect.py:188`) is the netloc minus `www.`, and every row in `body_fetch`
already carries its `source_slug`. Both detection points therefore know the slug directly
and must record **the slug, not `urlsplit(url).netloc`**. Deriving a slug from a request URL
at write time is the one way to get a name that `pause_for_pass` will not recognise.

## The work

### R1. Carry the status code out of the body fetch

Files: `src/bodies.py`, `src/polite_http.py`.

`_fetch_public` is the only rung that sees the status code, content type and final URL —
the rungs above it receive markup and nothing else. It must pass a throttle response up as
a throttle rather than flattening it into an `HTTPError` string. Do not recover the code by
parsing `BodyResult.error`: that field is the human-readable diagnosis and is displayed in
the admin UI, so a regex over it would couple the retry logic to the wording.

Reuse `THROTTLE_STATUSES` from `vendor/newscrawler/crawler_html_utils` rather than a second
list of codes, and reuse `retry_after_seconds` from `src/polite_http.py` rather than a
second `Retry-After` parser — it already handles both the delta and the HTTP-date form.

**Done when:** a 429 or 503 on a body fetch is distinguishable from an ordinary failure at
the call site, carrying its `Retry-After` when the publisher sent one.

### R2. A throttle must not spend the URL's attempt budget

Files: `src/bodies.py`.

This is the change that actually prevents data loss, and it is independent of the cooldown
file below. Without it a cooldown only slows the bleeding: the host is cooled, comes back,
is throttled again, and after five such cycles the queue retires anyway.

A throttle says nothing about whether the URL is fetchable, so it belongs with the `untried`
case, not with an ordinary failure — but unlike a transport error it is trustworthy evidence
even after the pass has succeeded elsewhere, because the host answered. Follow the existing
`untried` branch for how an attempt is withheld and how the pass is ended; do not rewrite
the transport breaker, which is solving a different problem (our network versus a dead host).

**Done when:** repeated 429s never move a `body_fetch` row to `unavailable`; a genuine
404/410 still retires immediately, and an ordinary retryable failure still counts toward five.

### R3. Persist the rejection

Files: `src/polite_http.py`, `src/collect.py`, `src/bodies.py`, new cooldown module.

Discovery already holds the fact in `PoliteAdapter.tripped` and throws it away at pass end;
surface it to the caller alongside the existing `http` dict that `run_collection` already
logs. Bodies get it from R1.

Store it in a JSON file — `data/publisher_cooldown.json` — not in the database. The
precedent is `health/canaries.json`: an operational switch belongs in one hand-editable file
that survives a database restore and that a person can clear at 23:00 without a SQLite
client. It must be **written beside the target and moved over it**, exactly as the batch
files do for `last_intraday_run.json`; slots are two hours apart, but a manual backfill can
overlap one and a half-read cooldown file is a bad failure.

Shape — an expiry per source, not a bare flag:

```json
{
  "dvz.de": {
    "until": "2026-09-17T22:00:00Z",
    "since": "2026-09-17T10:04:11Z",
    "reason": "HTTP 429 from www.dvz.de (2 throttle response(s)), stopped this pass",
    "stage": "collect",
    "consecutive_days": 1
  }
}
```

An expiry rather than "clear it at midnight" because nothing runs between 23:00 and 05:00,
so there is no process to do the clearing. Default cooldown is the rest of the day: the last
intraday slot is 22:00, so expiring then leaves the 06:00 daily run free to retry once
overnight, which is the gentlest possible probe of whether the block has lifted. Honour a
longer `Retry-After` when the publisher sent one — that is the publisher naming its own
number and it outranks our default. Record the timestamps in UTC, as the markers do;
collection's Berlin dates are a date-filtering concern and do not belong here.

`consecutive_days` is not decoration; R5 reads it.

**Done when:** a throttle in either stage writes an entry atomically, with the publisher's
`Retry-After` preferred over the default expiry, and the file is valid JSON after a pass
that was killed mid-write.

### R4. Consume it in `run.py`, not in the batch file

Files: `run.py`, `run_intraday.bat` and `run_daily.bat` (unchanged, deliberately).

`run.py` reads the cooldown file itself and folds the unexpired slugs into the `exclude`
list it already passes to `run_collection` and `run_body_fetch`. The `--exclude` flag stays
for manual use and for the ZEIT-style long pause.

Do **not** plumb this through the batch files. The `:stage` helper forwards `%2` through
`%9` — eight arguments — which is why `pause_for_pass` accepts a comma-separated form at
all, and rendering JSON into a command-line flag from `cmd.exe` is worse than reading the
file in Python. Neither `.bat` should change for this feature.

One sharp edge: `pause_for_pass` raises `ValueError` on a name matching no configured source
(`src/collect.py:234`). That is right for a hand-typed `--exclude`, where a typo would
otherwise silently exclude nothing forever. It is wrong for a machine-written file, where it
aborts the entire collect stage because of one stale entry — a source renamed or removed
from `germany_medias.json` leaves exactly such an entry behind. The file reader must drop
unknown slugs with a WARNING and pass on the rest; keep `ValueError` for the flag.

Expired entries are skipped on read and pruned on write, so the file does not grow forever.

**Done when:** a slot with a live cooldown entry sends that publisher no request from either
stage and reports it `paused`; an entry naming a removed source logs a warning and costs
nothing; an expired entry is ignored and disappears.

### R5. The alarm — the point of the whole thing

Files: `health/analyze.py`, `health/check.py` on the VPS, cooldown module.

A cooled source reports `paused`, and health deliberately treats `paused` as fine — that is
the ZEIT design and it should not change. The consequence is that this feature, built alone,
is a very tidy way to stop noticing a publisher. Everything above is only safe because of
this section.

Rejections are reviewed **by hand**, so the alarm is the deliverable and the cooldown is
what buys the time to answer it. Two distinct signals, because they mean different things:

- **A new rejection.** Fires on the pass that writes an entry, naming the publisher, the
  stage, the status and the reason string. This is the one that wants to reach a person the
  same day — a first 429 from a publisher we have never been throttled by is the early
  warning that the ZEIT block did not give us.
- **A stale cooldown.** Fires when `consecutive_days` crosses a small threshold — a source
  that has been cooled every day for several days is not being throttled, it is blocking us,
  and no amount of waiting will fix it. This must escalate rather than repeat, because a
  daily line saying the same thing is the failure mode that stops being read.

Both belong with the existing source accounting so they travel the path that already works:
`health/analyze.py` writes its own atomic JSON, and the VPS's `brandmonitor-health.timer`
reads the markers over SSH every fifteen minutes. Reuse the existing latch — the
`run_failed` notification already delivers by email *or* push, and a second notification
channel is not wanted.

Data loss from a long cooldown is accepted and is explicitly **not** handled here. Some
sources are pinned to news-only sitemap files with a short listing window and will lose
articles across a multi-day pause; that is what the stale-cooldown alarm exists to surface,
and the recovery is a deliberate `run.py probe` / `tools/rediscover.py` backfill by a person
once the publisher relationship is sorted out. Do not build automatic backfill for this.

**Done when:** a first rejection reaches a person the same day; a source cooled for several
consecutive days escalates once rather than repeating; a cleared cooldown is visible as
resolved; and neither alarm fires for an ordinary `--exclude` pause.

## Order and dependencies

R1 → R2 → R3 → R4 → R5. R2 is the one that must not be skipped if the others slip: it is
small, it is what stops a temporary block from retiring a queue, and it stands alone without
the cooldown file. R5 is the one that must not be skipped if R2 lands: without it the
cooldown makes a blocked publisher invisible.

Two sources share one publisher — `haendlerbund.de` and `ohn.haendlerbund.de` are separate
slugs. If they sit behind one rate limiter, cooling one while the other keeps requesting
defeats the exercise. Check this before assuming slug granularity is sufficient; the fix, if
needed, is an optional `cooldown_group` in the source entry rather than host inference.

## Before intraday is re-enabled

`crawl_tasks.md` records intraday as disabled, verified in Task Scheduler, and both C3 and
W20 name resuming it as the thing they gate. This file is that gate. Re-enable the
`brandmonitor-intraday` task only after R2 and R5 are live — a code pull does not enable it,
and it must be turned on deliberately.
