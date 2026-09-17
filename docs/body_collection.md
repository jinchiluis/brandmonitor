# Body collection

`collect` now commits discovered hints and then fetches a bounded batch of public
article bodies. This needs no client profile or assessment prompt.

## Source policy

Discovery and body fetching share `data/publisher_cooldown.json`. A body HTTP
403/429/503 pauses that source for 23 hours, or longer when `Retry-After` says so,
without consuming a URL attempt. Remaining tasks for that source are skipped in
the current pass; other sources continue. The next pass checks cooldowns before
queuing or fetching. HTTP 404/410 still retire immediately and ordinary retryable
failures still count toward the attempt limit. The daily and intraday coverage
analyzer reports rejections through the existing VPS health notifications.
See [rejection_plan.md](../rejection_plan.md) for expiry, escalation and manual clearing.

Each source has `content_mode` in its JSON entry. `full_text` is configured for 15
trade/association sources and all 8 regulatory sources; 9 news sources use
`title_only`. Missing settings default to `title_only`; invalid values
fail before collection starts.

`excluded_dirs` removes a path prefix across discovery methods and is also enforced
when selecting stored rows for body backfill. `excluded_url_substrings` and
`excluded_title_substrings` handle rolling indexes that share a section with real
articles; URL substrings are matched against the path and the query string.
Discovery, body backfill, and candidate selection enforce all three. They
are stricter than `allowed_dirs`, whose semantics differ by discovery method.
LOGISTIK HEUTE uses them to exclude `/fachmagazin`, event listings, and company
`Newsübersicht` pages; BPEX uses `/aktuelles?` for pagination and PDF copies that
sit on the bare section path beside real `/aktuelles/meldung/` items.

## Page dates

The extractor reads structured attributes only, in this order: JSON-LD
`datePublished`, `<meta property="article:published_time">`, then a `<time datetime>`
when every dated `<time>` on the page names the same day. Modification dates are
deliberately ignored - they are `lastmod` under another name - and visible text is
never parsed, because the first visible date on a page was a future seminar on one
sampled source and a listing entry on another.

The lone-`<time>` rule is what dates bevh (one `<time itemprop="datePublished">`)
and DSLV (one `<time>` holding epoch seconds). It refuses pages with several
disagreeing `<time>` elements rather than guessing: a Verbraucherzentrale class
action carries filed, served and status dates and none is a publication date; a
BPEX listing carries one per teaser; an EDPB article carries its own plus one per
related-news card, and the feed already dates it. Durations such as `PT5M` and
empty attributes do not count either way.

Page dates are accepted only after validation and normalization to ISO 8601.
LOGISTIK HEUTE's Drupal JSON-LD emits German-weekday, US-month/day values such as
`Do, 09/10/2026 - 14:33`; this becomes `2026-09-10T14:33:00+02:00`, while the
publisher string is retained in `published_at_raw`. An invalid page value is kept
as raw evidence and the discovery date remains authoritative.

`tests/test_page_dates.py` pins the expected value for twelve reduced captures of
real publisher pages under `tests/fixtures/dates/`; `capture.py` there re-captures
them. A template change fails that test instead of silently demoting a source to
`lastmod`.

**htmldate is not used for stored dates, and should not be reached for.** It is
already installed as a trafilatura dependency, and its fast mode does get several
templates right, which makes it tempting. It reads text from any element whose
class contains "date": it confidently dated every hub page in the 2026-09-11
sample from the teaser dates on it, and took a class action's filing date as the
publication date. Both failures produce a plausible wrong date rather than no
date, which is the one outcome this pipeline cannot tolerate. It may be useful as
a diagnostic column in `probe`, never as a stored value.

Not every undated row is an extraction failure. Regulators (Bundeskartellamt,
vzbv, BEUC, the Commission press corner, HDE) state no structured date and are
dated by their feed; Bundesnetzagentur's feed carries no date either and its press
releases stay undated; BPEX article pages carry no date at all, only the listing
does. Those are accepted as undated: they are not daily news, and assessment reads
the dates in the body. A page-date share of zero on such a source is expected, not
a bug.

The body stage is an **escalation ladder**, not one request. Each rung costs more
than the one below it, so each runs only when the cheaper rung failed in the one
way that rung can fix:

| Rung | Runs when | Cost |
|---|---|---|
| 1. plain HTTP + trafilatura | always | one paced request |
| 2. PDF text layer (pypdf) | the bytes begin `%PDF-` | negligible |
| 3. headless browser (Playwright) | rung 1 returned *no usable article text* | seconds |
| 4. subscriber login | a declared paywall **and** credentials in `.env` | a subscription |

The ordinary article therefore stays a single paced HTTP request. Rung 1 is the
only one that sees the status code, content type and final URL, which is what
classifies an outcome as `unavailable` (stop) rather than `failed` (retry) - the
rungs above it receive markup and nothing else.

The vendored `scraper_fetch_html.fetch_html` was deliberately **not** the entry
point, and was deleted on 2026-09-16 once that was settled. It returned markup
only, discarding the metadata that classification needs; it attempted a paywall
login before anything else, which would launch a browser for every ZEIT, WELT and
SPIEGEL URL whether or not a subscription exists; and it made up to eight requests
per URL before AMP variants, which would undo the per-host pacing. Its useful
leaves - `fetch_html_with_playwright` and `paywall/handler.py` - survive and are
called directly by the rungs that need them.

PDF library: **pypdf** (BSD-3). pymupdf extracts better but is AGPL, which does
not suit a commercial deliverable without a paid licence.

Content type is decided on the **bytes**, not the header: servers mislabel, and a
PDF served as `text/html` or HTML served as `application/pdf` both extract
correctly.

The setting requests full text; it does not imply every page is accessible.

## Commands

```powershell
# Discover and store hints, then attempt the configured body batch.
python run.py collect --kind news
python run.py collect --kind regulatory --body-limit 20

# Backfill existing stored hints or retry failures without rediscovering URLs.
python run.py fetch-bodies --kind regulatory --limit 20
python run.py fetch-bodies --kind news --limit 100

# Fetch only title-only URLs kept by this client's title-gate JSONL decisions.
python run.py fetch-bodies --kind news --title-gate-client jt-express

# Recheck successful fetches, even when discovery metadata has not changed.
python run.py fetch-bodies --kind news --refresh --limit 20

# Explicitly revisit unavailable pages.
python run.py fetch-bodies --kind regulatory --retry-unavailable --limit 20
python run.py status
```

Both collection commands and `fetch-bodies` apply pending migrations automatically.
`config.json` supplies `body_fetch.limit` (currently 1000) and `timeout_seconds` (20, with a
5-second connection timeout). Body requests run sequentially. A limit bounds the
number attempted, not the number discovered or the total available archive.
Repeat `fetch-bodies` to work through the reported backlog. Previously attempted
URLs sort after untouched ones, with the oldest attempt first among retries.

`--title-gate-client` changes eligibility from all configured `full_text` URLs to
explicit keeps in `data/title_gate/<client>/*.jsonl`. Those keeps may enter the
queue despite `content_mode=title_only`; no other URL from those sources does. Once
queued, retry state is durable in `body_fetch`, so a failed URL remains retryable
after the dated JSONL log is pruned. Its `hint_payload.title_gate_routes` retains
the client and original selector reasons so the fetched body can go directly to
that client's full assessor. The title gate already made the cheap relevance
decision; the body is enrichment, not a reason to gate the item a second time.

The backfill respects the current `allowed_dirs` for sitemap discoveries, including
the already narrowed Verbraucherzentrale sections. Other discovery methods retain
their existing section semantics.

## Storage and versions

- `raw_item.payload.body_text` stores extracted text with paragraph breaks.
  Successful payloads also have `body_status`, `body_fetched_at`, `body_url` (the
  final response URL), and `body_extractor`. The URL used for identity stays stable.
- Initial body enrichment appends a version, preserving the original hint and any
  assessment attached to it. Rechecks compare normalized title and text with the
  latest version. Changes append a new version; unchanged text, whitespace changes,
  and sitemap date churn do not. A genuine A → B → A reversion is preserved.
- `published_at` is the page date when the page states one, otherwise the
  discovery date. `payload.published_at_source` says which: `page`, or the
  discovery field that supplied it - `feed` (`<pubDate>`), `news_sitemap`
  (`<news:publication_date>`), `lastmod`, `frontpage` - or null when undated.
  Collection writes the discovery label for every row, title-only included; a
  body fetch overwrites it with `page`. Rows collected before 2026-09-11 carry the
  older `discovery` label, which readers resolve from `discovered_via`: `rss`
  means `feed`, `sitemap` stays `sitemap` because a news-sitemap date and a
  `lastmod` were not told apart then. Relabelling is not a content change and
  never versions a row. Neither the date nor the fetch time is in the body hash.
- `body_fetch` holds mutable per-URL fetch state, hint metadata, the count of
  consecutive unsuccessful attempts, attempt time, last run, and error. It is
  separate from immutable raw versions. For title-only keeps its hint also carries
  the client route and original selector reasons; this is queue routing, not source
  content or a relevance assessment. A failed refresh leaves the previously stored
  body intact.
- Changed discovery metadata queues a recheck, rather than creating another
  headline-only version. Identical hints do not refetch an already successful body.
  Unsignalled page changes require `--refresh`.

The original hint's `body_status=pending` describes that historical version. Read
`body_fetch.status` for the current fetch outcome. Assessment is still a stub; when
it is implemented, candidate selection must handle the latest source version and
body availability rather than assess every historical hint and enriched version.

## Outcomes

| State | Meaning | Retry |
|---|---|---|
| `pending` | Not fetched yet, or discovery metadata changed | Next batch |
| `ok` | Usable text extracted | Changed hint or `--refresh` |
| `failed` | HTTP/network error, access challenge, insufficient text, or a browser that could not navigate | Next batch, until `max_attempts` consecutive failures |
| `unavailable` | Paywall, missing page, unsupported media, homepage, scanned PDF, **no article even after rendering**, or retired after `max_attempts` failures | Changed hint or `--retry-unavailable` |

`body_fetch.max_attempts` (config.json, 5) bounds the retries. Past it a failure is
stored as `unavailable` with `gave up after N attempts: <last error>`, so the
diagnosis stays visible and the row stays recognisable. A success resets the count,
and so does a changed discovery hint, which reopens the URL with a fresh budget.
The cap must never bury a URL that an extraction fix would recover - that is how
the VerkehrsRundschau articles were lost - so after changing the extractor run
`--retry-unavailable` for the affected source; the retired rows are reopened like
any other unavailable row. Measured before the cap existed: the whole queue held
one `failed` row and a maximum of four attempts anywhere, so this is a guard
rail, not a rescue.

Discovery and body runs are recorded separately (`news`/`regulatory` and
`bodies:news`/`bodies:regulatory`). Discovery may advance its watermark once hints
and queue entries are committed: a body retry no longer depends on an item remaining
inside a feed or crawl window. Body runs report attempts, new versions, errors,
unavailable pages, and deferred work. CLI exit status is nonzero only when the
current batch encounters retryable technical failures. Expected terminal
`unavailable` outcomes remain visible without failing the run; deferred work is
reported as backlog.

## Parliamentary documents

The documents behind DIP procedures are not bodies of their own items and do not
go through `body_fetch`: a step references a Drucksache by DIP document id, and one
collective Drucksache of written questions serves many procedures.
`python run.py fetch-dip-docs --client <client>` queues every Drucksache the newest
version of a procedure references once the client's regulatory body gate has
judged the procedure relevant, then fetches what is due, newest first, up to
`--limit` (50). The text is stored whole in `dip_document`, one row per document id
and shared by every client; the cut the full assessment reads is made when it is
read (`src/dip_documents.py`).

| State | Meaning | Retry |
|---|---|---|
| `pending` | Queued, or DIP has no text for it yet | Next run |
| `ok` | Text stored | Never. A Bundestag advance copy (*Vorabfassung*) is later replaced by the edited version; the difference is editorial |
| `failed` | HTTP or network error | Next run |
| `unavailable` | Without text or failing for 14 consecutive runs | `--retry-unavailable` |

DIP publishes a Drucksache's text days after its date, so a missing text is the
normal state of a fresh answer, not a failure, and it does not change the exit
code. The queue owns retries: a document once queued is asked for until it has
text or is given up, whatever happens to the decision that queued it. Runs are
recorded as `dip_documents`. Exit 1 means every request in the batch failed, exit 2
a rejected API key. `--show <procedure id>` prints one procedure's record and cut
documents without fetching anything.

## Validation, 2026-09-09

Automated tests cover immutable enrichment, date churn, real changes and reversions,
whitespace, retries outside the discovery window, source tiers, scoped backfills,
batch limits, and extraction of quotations. Existing collection tests remain green.

Live validation used a separate database under `data/body_validation/`. A discovery
run over etailment and LOGISTIK HEUTE exposed a homepage in sitemap results and a
declared paywall. The homepage is now rejected before fetching; the paywall is
recorded as unavailable. A vzbv quotation inside a slider exposed an extraction
omission; the extractor now preserves semantic blockquotes in those wrappers.

Four article pages (etailment, LOGISTIK HEUTE, EDPB, vzbv) were then fetched and
inspected. All four yielded usable text. A repeat attempted zero fetches; a forced
refresh fetched all four and wrote zero additional versions. This is a sample,
not validation of extraction quality across all configured sources.

**Ladder verified live 2026-09-09** against failures already in the database:

- BNetzA Amtsblatt PDF - **6,278 words** extracted; previously "unsupported
  content type".
- BIEK/BPEX press release PDF - **393 words**; the parcel and express logistics
  association, squarely the client's sector, previously discarded.
- EU Commission presscorner - **807 words** via the browser rung. That source was
  38 of 38 failing, i.e. entirely dark.
- Three pages stayed empty after rendering (a DSLV section listing, a DVZ webinar
  form, a bevh position stub) and are now `unavailable` rather than retried.

Two traps the live run exposed, both fixed:

- A page that renders and *still* has no article is not an article - a listing, a
  form, a stub. Left retryable it would cost ~20s of browser time on every run
  forever, so it is marked `unavailable`.
- `fetch_html_with_playwright` **swallows navigation errors** and returns whatever
  the browser is showing, which for an unreachable host is a 39-byte blank
  document. Treating that as "no article" would permanently retire a URL over a
  DNS blip, so a blank document is a retryable transport failure instead.

Authenticated content is wired but inert: no credentials exist in `.env`, and
`paywalls.json` covers SPIEGEL, ZEIT and WELT but not FAZ, Handelsblatt,
Süddeutsche or WiWo.
