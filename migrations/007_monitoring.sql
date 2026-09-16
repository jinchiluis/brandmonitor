-- Operational monitoring (src/monitoring.py). These tables record what the
-- pipeline did - which pass ran, what each source's discovery sent and read, what
-- each body attempt returned - so a problem can be seen per source and per file
-- instead of reconstructed from log text. Nothing in the pipeline reads them back:
-- they are written after the production commit they describe, in their own
-- transaction, and a failure to write them never changes an outcome.

-- One batch invocation: run_daily.bat or run_intraday.bat, including the slots
-- that did no work because the host was offline or another pass held the lock.
-- Stage runs belong to the pass whose [started_at, finished_at] contains them;
-- the run lock makes scheduled passes disjoint.
CREATE TABLE pipeline_pass (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,                 -- 'daily' | 'intraday'
    outcome       TEXT NOT NULL,                 -- 'completed' | 'offline' | 'lock_skipped'
    started_at    TEXT,                          -- UTC; null if the batch could not tell
    finished_at   TEXT NOT NULL,                 -- UTC
    cycle_date    TEXT,                          -- the log directory's local date
    worst_exit    INTEGER,
    stages        TEXT NOT NULL DEFAULT '{}',    -- JSON {stage: exit code}
    log_path      TEXT,
    git_commit    TEXT,
    git_dirty     TEXT,                          -- JSON list of modified tracked files, null if unknown
    config_hash   TEXT,                          -- sha256 over config_files
    config_files  TEXT,                          -- JSON {relative path: sha256}
    host          TEXT,
    recorded_at   TEXT NOT NULL
);

CREATE INDEX idx_pipeline_pass_started ON pipeline_pass (started_at);

-- One source's discovery within one collection run: what it cost, the window it
-- asked for, and whether its watermark moved. run_source keeps the outcome;
-- this keeps the measurements behind it.
CREATE TABLE discovery_source (
    run_id              INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    source_slug         TEXT    NOT NULL,
    status              TEXT    NOT NULL,        -- as run_source.status
    started_at          TEXT,                    -- UTC
    finished_at         TEXT,                    -- UTC
    window_start        TEXT,                    -- the source's start (its watermark unless --days)
    window_end          TEXT,
    since               TEXT,                    -- window_start minus the overlap: the date filter
    watermark_before    TEXT,
    watermark_after     TEXT,
    watermark_advanced  INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER,                 -- requests started, including those with no response
    responses           INTEGER,                 -- requests answered (PoliteAdapter.requests)
    transport_errors    INTEGER,                 -- requests with no response at all
    not_modified        INTEGER,
    replayed            INTEGER,
    bytes               INTEGER,
    throttles           INTEGER,
    hints               INTEGER,                 -- after dedupe and exclusion, as collection counts them
    excluded            INTEGER,                 -- dropped by the source's exclusion rules
    index_pages         INTEGER,                 -- dropped as tag/author/pagination pages
    malformed           INTEGER,
    stored              INTEGER,
    error               TEXT,                    -- as run_source.error
    PRIMARY KEY (run_id, source_slug)
);

-- One HTTP request a discovery pass started, whatever became of it, and - for a
-- sitemap or feed - what the file yielded. A request that got no response has a
-- null status and an error; a redirect is one row per hop, and the parse result
-- sits on the final hop.
CREATE TABLE fetch_event (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    source_slug      TEXT    NOT NULL,
    seq              INTEGER NOT NULL,           -- order within the source's pass
    at               TEXT    NOT NULL,           -- UTC, when the request started
    method           TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    redirected_from  TEXT,                       -- the URL originally asked for
    conditional      INTEGER NOT NULL DEFAULT 0, -- sent If-None-Match / If-Modified-Since
    status           INTEGER,
    elapsed_ms       INTEGER,
    bytes            INTEGER,
    content_type     TEXT,
    error            TEXT,
    parse            TEXT,                       -- 'parsed' | 'replayed' | null (not read as a sitemap or feed)
    entries          INTEGER,
    children         INTEGER,                    -- child sitemaps an index listed
    dated_entries    INTEGER,
    entries_since    INTEGER,                    -- entries dated at or after the pass's since
    newest_entry     TEXT,
    oldest_entry     TEXT
);

CREATE INDEX idx_fetch_event_run ON fetch_event (run_id, source_slug, seq);
CREATE INDEX idx_fetch_event_url ON fetch_event (url, at);

-- One body fetch attempt and what the queue made of it.
CREATE TABLE body_attempt (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    source_slug   TEXT    NOT NULL,
    external_id   TEXT    NOT NULL,
    at            TEXT    NOT NULL,              -- UTC, when the attempt started
    elapsed_ms    INTEGER,
    status        TEXT    NOT NULL,              -- 'ok' | 'failed' | 'unavailable', as written to body_fetch
    error         TEXT,
    transport     INTEGER NOT NULL DEFAULT 0,    -- no response at all
    counted       INTEGER NOT NULL DEFAULT 1,    -- 0 when it did not spend an attempt (outage rule)
    attempts      INTEGER,                       -- body_fetch.attempts after this attempt
    final_url     TEXT,
    stored        INTEGER NOT NULL DEFAULT 0     -- new raw_item versions written
);

CREATE INDEX idx_body_attempt_run ON body_attempt (run_id);
CREATE INDEX idx_body_attempt_item ON body_attempt (source_slug, external_id, at);
