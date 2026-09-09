-- Minimal data boundary from mvp_plan.md section 4.
--
-- Four things only: pipeline runs with per-source status, versioned raw source
-- items, client assessments tied to a prompt/profile version, and reports.
-- Regulatory and news payloads differ in shape and are NOT forced into one
-- business schema - raw_item.payload is JSON whose shape depends on source_kind.

CREATE TABLE run (
    id            INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL,          -- 'news' | 'regulatory'
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    status        TEXT    NOT NULL,          -- 'running' | 'ok' | 'failed'
    window_start  TEXT,
    window_end    TEXT,
    note          TEXT
);

-- Per-source outcome. mvp_plan requires every configured source to report
-- success, failure, or explicit zero yield - 'zero' is a real result, not an
-- absence of one, and must be distinguishable from 'failed'.
CREATE TABLE run_source (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    source_slug  TEXT    NOT NULL,
    status       TEXT    NOT NULL,           -- 'ok' | 'zero' | 'failed'
    items_found  INTEGER NOT NULL DEFAULT 0,
    items_stored INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    UNIQUE (run_id, source_slug)
);

-- Normalised source material, stored before any client prompt touches it.
-- external_id is the stable identity within a source: a normalised URL for news,
-- an API record id for structured sources.
CREATE TABLE raw_item (
    id           INTEGER PRIMARY KEY,
    source_slug  TEXT    NOT NULL,
    source_kind  TEXT    NOT NULL,           -- 'news' | 'safety_gate' | 'dsa' | ...
    external_id  TEXT    NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    url          TEXT,
    title        TEXT,
    published_at TEXT,
    fetched_at   TEXT    NOT NULL,
    first_run_id INTEGER REFERENCES run(id),
    content_hash TEXT    NOT NULL,
    payload      TEXT    NOT NULL,           -- JSON, shape per source_kind
    UNIQUE (source_slug, external_id, version)
);

CREATE INDEX idx_raw_item_published ON raw_item (published_at);
CREATE INDEX idx_raw_item_source    ON raw_item (source_slug, published_at);

-- Client relevance is never stored on the shared raw record.
CREATE TABLE assessment (
    id               INTEGER PRIMARY KEY,
    raw_item_id      INTEGER NOT NULL REFERENCES raw_item(id) ON DELETE CASCADE,
    client_slug      TEXT    NOT NULL,
    prompt_version   TEXT    NOT NULL,
    profile_version  TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    relevant         INTEGER,                -- 0/1/NULL when undecided
    payload          TEXT    NOT NULL,       -- JSON, the structured assessment
    UNIQUE (raw_item_id, client_slug, prompt_version, profile_version)
);

CREATE INDEX idx_assessment_client ON assessment (client_slug, relevant);

CREATE TABLE report (
    id           INTEGER PRIMARY KEY,
    client_slug  TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- 'weekly' | 'monthly' | 'alert'
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    path         TEXT,
    note         TEXT
);

-- Collection and analysis advance independently: an item is collected once
-- stored, and analysed once its assessment is stored. One watermark per scope,
-- e.g. 'collection:news', 'analysis:jt-express:news'.
CREATE TABLE watermark (
    scope      TEXT PRIMARY KEY,
    position   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
