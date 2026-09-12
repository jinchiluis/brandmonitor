-- Documents behind the steps of DIP procedures a client's body gate kept: the
-- government's answer, the bill, the committee report. One row per DIP document
-- id, shared by every client - a client's decision only says what gets fetched.
-- The text is stored whole; what the full assessment reads is cut from it when
-- it is read (src/dip_documents.py). Fetch state sits beside the text because a
-- Drucksache does not change once DIP has its text.
CREATE TABLE dip_document (
    document_id     TEXT PRIMARY KEY,         -- the fundstelle id DIP gives a step
    dokumentnummer  TEXT,                     -- 21/7052, 446/26
    drucksachetyp   TEXT,
    herausgeber     TEXT,                     -- BT | BR
    datum           TEXT,
    pdf_url         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ok', 'failed', 'unavailable')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    attempted_at    TEXT,
    error           TEXT,
    fetched_at      TEXT,
    last_run_id     INTEGER REFERENCES run(id),
    text            TEXT
);

CREATE INDEX idx_dip_document_status ON dip_document (status, datum);
