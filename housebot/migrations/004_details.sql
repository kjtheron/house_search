-- Fields from each listing's own page (the "details" fetch), plus the fetch queue state.
ALTER TABLE listings ADD COLUMN parking INTEGER;
ALTER TABLE listings ADD COLUMN storeys INTEGER;
ALTER TABLE listings ADD COLUMN ensuites INTEGER;
ALTER TABLE listings ADD COLUMN rates INTEGER;
ALTER TABLE listings ADD COLUMN levies INTEGER;
ALTER TABLE listings ADD COLUMN pets INTEGER;
ALTER TABLE listings ADD COLUMN features TEXT;           -- JSON list of normalized names
ALTER TABLE listings ADD COLUMN listed_at TEXT;          -- the site's listing date
ALTER TABLE listings ADD COLUMN detail_fetched_at TEXT;  -- NULL = still waiting for details
ALTER TABLE listings ADD COLUMN detail_attempts INTEGER NOT NULL DEFAULT 0;
