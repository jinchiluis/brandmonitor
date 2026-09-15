-- Conditional requests for sitemap and feed files (src/polite_http.py).
-- A 304 must yield what the unchanged file yielded before, not nothing: a body
-- recheck is queued whenever an article's discovery hint changes, so a feed that
-- went quiet while its sitemap twin did not would re-fetch bodies. Each row keeps
-- the file's validators together with its entries dated from `since` onward, and
-- is replayed only for a window starting no earlier than that.
CREATE TABLE discovery_cache (
    source_slug    TEXT NOT NULL,
    url            TEXT NOT NULL,
    etag           TEXT,
    last_modified  TEXT,
    since          TEXT NOT NULL,
    entries        TEXT NOT NULL,   -- JSON [[url, iso date|null, title|null, date_source|null], ...]
    children       TEXT NOT NULL,   -- JSON [[sitemap url, iso lastmod|null], ...]
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (source_slug, url)
);
