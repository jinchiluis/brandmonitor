-- News alert-gate decisions and delivery state.  This stays separate from
-- assessment: that table is the weekly funnel's relevance ledger, while an
-- alert is an additional notification over items already admitted by it.
CREATE TABLE alert_decision (
    id               INTEGER PRIMARY KEY,
    raw_item_id      INTEGER NOT NULL REFERENCES raw_item(id) ON DELETE CASCADE,
    source_slug      TEXT    NOT NULL,
    external_id      TEXT    NOT NULL,
    client_slug      TEXT    NOT NULL,
    prompt_version   TEXT    NOT NULL,
    profile_version  TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    potential_alert  INTEGER NOT NULL CHECK (potential_alert IN (0, 1)),
    summary_zh       TEXT    NOT NULL DEFAULT '',
    payload          TEXT    NOT NULL,
    sent_at          TEXT,
    email_message_id TEXT,
    UNIQUE (source_slug, external_id, client_slug, prompt_version, profile_version)
);

CREATE INDEX idx_alert_decision_pending
    ON alert_decision (client_slug, potential_alert, sent_at);
