-- Mutable fetch bookkeeping is separate from immutable source versions.
-- Discovery can advance after storing a hint: body retries survive its window.
CREATE TABLE body_fetch (
    source_slug     TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    discovery_hash  TEXT NOT NULL,
    hint_payload    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ok', 'failed', 'unavailable')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    attempted_at    TEXT,
    error           TEXT,
    last_run_id     INTEGER REFERENCES run(id),
    PRIMARY KEY (source_slug, external_id)
);

CREATE INDEX idx_body_fetch_status ON body_fetch (status, attempted_at);
