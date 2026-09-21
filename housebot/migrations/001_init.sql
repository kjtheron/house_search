-- Initial schema (plan §6.1). Applied once by housebot/db.py migrate(); never edit after
-- release, add 002_*.sql instead.

CREATE TABLE listings (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,   -- short id shown to user, e.g. #142
  source           TEXT NOT NULL,
  source_listing_id TEXT NOT NULL,
  url              TEXT NOT NULL,
  title            TEXT,
  province         TEXT,
  town             TEXT,
  suburb           TEXT,
  property_type    TEXT,
  listing_kind     TEXT DEFAULT 'sale',                 -- sale | auction
  price            INTEGER,                              -- ZAR, NULL if POA
  beds             REAL,
  baths            REAL,
  garages          INTEGER,
  floor_m2         INTEGER,
  erf_m2           INTEGER,
  agency           TEXT,
  agent_name       TEXT,
  description      TEXT,
  photo_url        TEXT,
  fingerprint      TEXT,                                 -- cross-source dedup key (§6.3)
  matches          INTEGER NOT NULL DEFAULT 0,           -- passes current config?
  status           TEXT NOT NULL DEFAULT 'active',       -- active | gone
  first_seen       TEXT NOT NULL,
  last_seen        TEXT NOT NULL,
  raw_json         TEXT,
  UNIQUE (source, source_listing_id)
);
CREATE INDEX idx_listings_fp ON listings(fingerprint);
CREATE INDEX idx_listings_town ON listings(town, suburb);

CREATE TABLE price_history (
  listing_id INTEGER NOT NULL REFERENCES listings(id),
  price      INTEGER,
  seen_at    TEXT NOT NULL
);

CREATE TABLE notifications (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id     INTEGER NOT NULL REFERENCES listings(id),
  fingerprint    TEXT,
  price_notified INTEGER,
  reason         TEXT NOT NULL,          -- new | price_change | relisted
  sent_at        TEXT NOT NULL,
  telegram_msg_id INTEGER
);

CREATE TABLE favourites (
  listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
  rating     INTEGER,                    -- 1..5 optional
  note       TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE hidden (                    -- "not interested", never notify again
  listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
  reason     TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, started_at TEXT, finished_at TEXT,
  seen INTEGER, new INTEGER, price_changes INTEGER, errors INTEGER,
  status TEXT                            -- ok | degraded | failed
);
