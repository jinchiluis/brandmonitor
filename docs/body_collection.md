# Body collection

`collect` now commits discovered hints and then fetches a bounded batch of public
article bodies. This needs no client profile or assessment prompt.

## Source policy

Each source has `content_mode` in its JSON entry. `full_text` is configured for the
16 trade/association sources and all 8 regulatory sources; the 8 mainstream news
sources use `title_only`. Missing settings default to `title_only`; invalid values
fail before collection starts.

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

The vendored `scraper_fetch_html.fetch_html` is deliberately **not** the entry
point. It returns markup only, discarding the metadata that classification needs;
it attempts a paywall login before anything else, which would launch a browser
for every ZEIT, WELT and SPIEGEL URL whether or not a subscription exists; and it
makes up to eight requests per URL before AMP variants, which would undo the
per-host pacing. Its useful leaves - `fetch_html_with_playwright` and
`paywall/handler.py` - are called directly by the rungs that need them.

PDF library: **pypdf** (BSD-3). pymupdf extracts better but is AGPL, which does
not suit a commercial deliverable without a paid licence.

Content type is decided on the **bytes**, not the header: servers mislabel, and a
PDF served as `text/html` or HTML served as `application/pdf` both extract
correctly.

The setting requests full text; it does not imply every page is accessible.

## Commands

```powershell
# Discover and store hints, then attempt up to 100 queued bodies by default.
python run.py collect --kind news
python run.py collect --kind regulatory --body-limit 20

# Backfill existing stored hints or retry failures without rediscovering URLs.
python run.py fetch-bodies --kind regulatory --limit 20
python run.py fetch-bodies --kind news --limit 100

# Recheck successful fetches, even when discovery metadata has not changed.
python run.py fetch-bodies --kind news --refresh --limit 20

# Explicitly revisit unavailable pages.
python run.py fetch-bodies --kind regulatory --retry-unavailable --limit 20
python run.py status
```

Both collection commands and `fetch-bodies` apply pending migrations automatically.
`config.json` supplies `body_fetch.limit` (100) and `timeout_seconds` (20, with a
5-second connection timeout). Body requests run sequentially. A limit bounds the
number attempted, not the number discovered or the total available archive.
Repeat `fetch-bodies` to work through the reported backlog. Previously attempted
URLs sort after untouched ones, with the oldest attempt first among retries.

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
- The original discovery timestamp is retained as `published_at`. Plain sitemap
  timestamps can still mean `lastmod`; this stage does not infer a true publication
  date. Neither that timestamp nor the fetch time participates in the body hash.
- `body_fetch` holds mutable per-URL fetch state, hint metadata, attempt count,
  attempt time, last run, and error. It is separate from immutable raw versions.
  A failed refresh leaves the previously stored body intact.
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
| `failed` | HTTP/network error, access challenge, insufficient text, or a browser that could not navigate | Next batch |
| `unavailable` | Paywall, missing page, unsupported media, homepage, scanned PDF, or **no article even after rendering** | Changed hint or `--retry-unavailable` |

Discovery and body runs are recorded separately (`news`/`regulatory` and
`bodies:news`/`bodies:regulatory`). Discovery may advance its watermark once hints
and queue entries are committed: a body retry no longer depends on an item remaining
inside a feed or crawl window. Body runs report attempts, new versions, errors,
unavailable pages, and deferred work. CLI exit status is nonzero when the current
batch encounters failures or unavailable pages; deferred work is reported as backlog.

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
