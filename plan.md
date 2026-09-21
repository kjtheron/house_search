# Western Cape House Hunter — Build Spec for a Coding Agent

**Goal:** A daily job on a Raspberry Pi that collects for-sale listings in the Western Cape from the main South African property sources, filters them against a user-defined config, stores everything in SQLite, and sends *new* matches (or matches whose *price changed*) to the owner on Telegram. A PicoClaw agent (LLM via Ollama Cloud) sits on top so the owner can chat on Telegram to mark favourites and query the database.

Read this whole document before writing code. Build in the phase order given. Each phase ends with a check that must pass before moving on.

---

## 1. Design principles (non-negotiable)

1. **Deterministic core, LLM on the edge.** Scraping, matching, dedup and notifying are plain Python on a timer. The LLM is *never* in the daily pipeline. PicoClaw only calls a CLI to read/write the DB. This keeps it cheap, testable and reliable.
2. **Polite, low-volume collection.** One run per day, one request every 5–10 s with random jitter, capped pages per source, honest User-Agent, respect `robots.txt`. Personal use only. Before enabling a source, read its Terms of Use; if a source forbids automated access, disable its web adapter and use its **saved-search email alerts** instead (see §4.3).
3. **Adapters are disposable.** Websites change HTML without notice. Every source is an isolated adapter with saved HTML fixtures and tests, so a break in one source never stops the others, and the owner is told on Telegram when an adapter breaks.
4. **Never re-notify the same house at the same price.** Enforced in the DB, not in memory.

---

## 2. Sources

Almost every Western Cape agency (franchise or independent) syndicates its stock to the big portals, so covering the portals covers most of the market. Agency websites are optional extras.

### 2.1 Tier 1 — portals (build these)

| Source | Notes |
|---|---|
| **Property24** (property24.com) | Largest SA portal by far. Listing ID is in the listing URL. Some automated traffic gets throttled; keep volume tiny. |
| **Private Property** (privateproperty.co.za) | Clear #2. Build second. |

### 2.2 Tier 2 — smaller portals (optional, add later)

ImmoAfrica (immoafrica.net), MyRoof (myroof.co.za), MyProperty (myproperty.co.za), Gumtree property section.

### 2.3 Tier 3 — large agencies with strong Western Cape presence (optional)

Pam Golding, Seeff, RE/MAX, Chas Everitt, Lew Geffen Sotheby's, Engel & Völkers, Harcourts, Rawson, Fine & Country, Just Property, Greeff, Jawitz, Dogon Group, Tyson Properties, Leapfrog. Most of their listings already appear on Tier 1, so only add one if the owner reports missing listings from it. Cross-source dedup (§6.3) handles the overlap.

### 2.4 Tier 4 — distressed / auction stock (optional)

Bank repossession sections on Property24, and auction houses (e.g. Auction Inc, High Street Auctions, Park Village Auctions, In2assets). Treat as separate sources with a `listing_kind = auction` flag.

---

## 3. Target environment

- Raspberry Pi 4 or 5 (2 GB+ RAM) recommended; Raspberry Pi OS **64-bit** (Bookworm or later). A Pi Zero 2 W can run the HTTP-only adapters and PicoClaw, but not a headless browser.
- Python 3.11+, SQLite 3 (bundled), `git`.
- Timezone: `Africa/Johannesburg`.
- PicoClaw installed and working with Telegram and an Ollama Cloud model before Phase 10.

### Python dependencies

`httpx` (HTTP/2, timeouts), `selectolax` or `beautifulsoup4` + `lxml` (HTML parsing), `pydantic` v2 (config + models), `pyyaml`, `tenacity` (retries), `python-dotenv`, `typer` (CLI), `pytest`. Optional: `playwright` (only if an adapter truly needs JS rendering; on Pi use system Chromium).

---

## 4. Architecture

```
            systemd timer (07:00 daily)
                      │
                      ▼
   housebot run ──► adapters ──► normalise ──► match ──► upsert DB ──► diff ──► Telegram notify
                     (P24, PP,                              │
                      email-alert)                          ▼
                                                        SQLite  ◄── housebot CLI ◄── PicoClaw (exec tool)
                                                                                         ▲
                                                                          Owner on Telegram (chat)
```

### 4.1 Repo layout

```
housebot/
  config.yaml            # user search spec (§5)
  .env                   # secrets (never commit)
  housebot/
    __init__.py
    cli.py               # typer app: run, search, show, fav, stats, sources
    config.py            # pydantic models + loader
    db.py                # connection, migrations, queries
    models.py            # Listing dataclass / pydantic model
    match.py             # filter logic
    notify.py            # Telegram sender
    fingerprint.py       # cross-source dedup
    http.py              # polite client (rate limit, jitter, retries, cache)
    adapters/
      base.py            # SourceAdapter protocol
      property24.py
      privateproperty.py
      email_alerts.py    # IMAP fallback
  migrations/001_init.sql
  tests/fixtures/<source>/*.html
  tests/test_*.py
  deploy/housebot.service, housebot.timer
  picoclaw/skills/housebot/SKILL.md
```

### 4.2 Adapter interface

```python
class SourceAdapter(Protocol):
    name: str                                   # "property24"
    def search(self, cfg: SearchConfig) -> Iterator[Listing]: ...
    def detail(self, listing: Listing) -> Listing: ...   # optional enrichment
```

Rules for adapters:
- Build search URLs from the config (province, town/suburb, price range, beds, type) so the site does most filtering; still re-check every filter locally in `match.py`.
- **Prefer structured data** over CSS selectors: look first for JSON-LD (`<script type="application/ld+json">`), embedded JSON state, or data attributes. Fall back to selectors only if none exist.
- Extract a stable `source_listing_id` (from the URL or page data).
- Fetch detail pages **only** for listings that are new or whose card price changed (saves requests).
- Paginate up to `max_pages` (default 10). Stop early when a page returns no new IDs.
- On any parse failure, log, skip that item, continue. If a whole source returns 0 results or >50 % parse failures, mark the run as `degraded` (§8).

### 4.3 Email-alert adapter (fallback / ToS-friendly route)

The owner creates saved searches with email alerts on Property24 and Private Property, sending to a dedicated mailbox (e.g. a Gmail account with an app password). The adapter:
1. Connects via IMAP, reads unread alert emails from known senders.
2. Extracts listing URLs + IDs from the email HTML.
3. Optionally fetches each listing page once (a handful of requests per day) for details.
4. Marks emails as read.

This path is the most robust against site changes and blocking, so build it even if web adapters work.

---

## 5. Config (`config.yaml`)

```yaml
search:
  province: western-cape
  towns: [Stellenbosch, Somerset West, Paarl]   # required, at least one
  suburbs: []                                    # optional narrower filter
  property_types: [house, townhouse]             # house | townhouse | apartment | vacant_land | farm
  price_min: 2000000                             # ZAR
  price_max: 4500000
  beds_min: 3
  baths_min: 2
  garages_min: 0
  floor_min_m2: 150                              # optional
  erf_min_m2: 400                                # optional
  include_keywords: []                           # e.g. [solar, borehole] — all must appear
  exclude_keywords: [retirement, "life right", "share block"]
  include_auctions: false
  unknown_values_pass: true                      # if a listing lacks floor size, does it pass?

sources:
  property24:     { enabled: true,  max_pages: 10 }
  privateproperty:{ enabled: true,  max_pages: 10 }
  email_alerts:   { enabled: false, imap_host: imap.gmail.com, folder: INBOX }

http:
  min_delay_s: 5
  max_delay_s: 10
  timeout_s: 30
  user_agent: "housebot/1.0 (personal use; contact: you@example.com)"

notify:
  max_per_run: 25          # overflow gets one summary message
  send_photo: true
  quiet_if_none: false     # send "No new matches today" message?

paths:
  db: /home/pi/housebot/data/housebot.db
```

Secrets in `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IMAP_USER`, `IMAP_PASSWORD`.

Validate with pydantic on load; fail fast with a clear error.

---

## 6. Database

### 6.1 Schema (`migrations/001_init.sql`)

```sql
PRAGMA journal_mode = WAL;

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
```

### 6.2 Upsert + change detection (per listing, in one transaction)

1. Look up `(source, source_listing_id)`.
2. If absent → insert, insert `price_history` row. Change type = `new`.
3. If present → update fields + `last_seen`, set `status='active'`. If `price` differs from the stored price → insert `price_history`, change type = `price_change`. If it was `gone` → change type = `relisted`.
4. After the source finishes a **complete** (non-degraded) run, mark listings from that source not seen for 3 consecutive runs as `gone`. Never mark `gone` after a degraded run.

### 6.3 Cross-source dedup (fingerprint)

The same house is often listed by several agents and portals. Fingerprint =
`sha1(lower(town) | lower(suburb) | property_type | beds | baths | round(erf_m2, -1) | round(floor_m2, -1))`.
Ignore null parts but require suburb + beds + at least one size. If no valid fingerprint can be built, use `source:source_listing_id`.

### 6.4 Notification rule (the core requirement)

Send a Telegram message for a listing **only if all** hold:
- `matches = 1` and `status = 'active'`
- not in `hidden`
- **no** row in `notifications` with the same `fingerprint` and `price_notified = current price`

Reason label: `new` if no notification exists for the fingerprint, otherwise `price_change` (include old → new price and % change). Record the notification row **after** Telegram confirms delivery, so a failed send is retried next run.

---

## 7. Telegram notifier

- Use the Bot API directly (`sendPhoto` with caption, or `sendMessage`), HTML parse mode, ≤ 1 message/second.
- The same bot token PicoClaw uses is fine: the notifier only *sends*; PicoClaw does the receiving. Do **not** call `getUpdates` from housebot (it would steal PicoClaw's messages). For the same reason, don't use inline buttons.
- Message format:

```
🏠 NEW  #142 · Stellenbosch, Die Boord
R 3 950 000 · House · 3 bed · 2 bath · 2 garage
Floor 185 m² · Erf 620 m²
Agency: Example Realty
https://www.property24.com/for-sale/...
Reply to me: "fav 142" or "hide 142"
```

  For price changes: `📉 PRICE DROP #142: R 4 200 000 → R 3 950 000 (−6.0 %)` (📈 for increases).
- If more than `max_per_run` matches, send the first N, then one summary line with the count and `housebot search --since today` hint.
- Also send a short daily run summary and any **degraded source alerts** (§8).

---

## 8. Health monitoring

After each run, per source: if `seen = 0`, or parse errors > 50 %, or HTTP 403/429 persists after retries → status `degraded`, send Telegram: `⚠️ property24 adapter degraded (0 listings). Site may have changed.` Save the failing HTML to `data/debug/<source>/<date>.html` for later fixing. On 403/429, back off: skip that source for the rest of the day; do not rotate proxies or evade blocking.

---

## 9. CLI (`housebot`) — the interface PicoClaw uses

All commands support `--json` for machine-readable output; human output otherwise.

| Command | Purpose |
|---|---|
| `housebot run [--source X] [--dry-run]` | Full pipeline. `--dry-run` = no Telegram, no notification rows. |
| `housebot search [--town] [--suburb] [--price-max] [--beds-min] [--since 7d] [--text "pool"] [--favs] [--include-gone] [--limit 20]` | Query stored listings. |
| `housebot show <id>` | Full detail + price history + notification history. |
| `housebot fav add <id> [--rating 4] [--note "..."]` / `fav rm <id>` / `fav list` | Favourites. |
| `housebot hide <id> [--reason]` / `unhide <id>` | Suppress future alerts. |
| `housebot stats` | Counts per town, median price, new this week, last run status. |
| `housebot sources` | Last run status per source. |
| `housebot config show` | Print active search config. |

Rules: only `fav`, `hide`, `unhide` write to the DB from the CLI (besides `run`). No command accepts raw SQL. Validate IDs as integers.

---

## 10. Build phases

**Phase 0 — Pi prep.** Update OS, set timezone, create `housebot` system user, clone repo to `/home/housebot/housebot`, create venv, install deps. *Check:* `python -c "import httpx, pydantic"` works.

**Phase 1 — Scaffold + config.** Repo layout, `config.py` with pydantic models, `.env` loading. *Check:* invalid config gives a readable error; `housebot config show` prints it.

**Phase 2 — Database.** Migration runner (store applied version in `PRAGMA user_version`), `db.py` helpers for upsert, price history, notifications, favourites. *Check:* unit tests for insert / unchanged / price change / relisted / gone.

**Phase 3 — Polite HTTP client.** Delay with jitter, timeouts, 3 retries with backoff on 5xx only, stop on 403/429, optional on-disk cache for dev. *Check:* test that two calls are ≥ `min_delay_s` apart.

**Phase 4 — Property24 adapter.**
1. Manually load one Western Cape search results page and one listing page; save to `tests/fixtures/property24/`.
2. Inspect for JSON-LD / embedded JSON first; write parser against the fixtures.
3. Implement search URL builder from config and pagination.
4. *Check:* fixture tests extract ID, price, beds, baths, sizes, suburb, URL for every card; one live `--dry-run` returns plausible listings.

**Phase 5 — Matching.** `match.py` applies every config filter locally, including keyword include/exclude on title + description, and the `unknown_values_pass` rule. *Check:* table-driven unit tests.

**Phase 6 — Fingerprint + notification rule.** Implement §6.3 and §6.4. *Check:* tests prove (a) same listing twice → one notification, (b) price change → second notification, (c) same house on two portals at same price → one notification, (d) hidden → none.

**Phase 7 — Telegram notifier.** Implement §7. *Check:* `housebot run` against fixtures sends formatted test messages to the owner's chat; second run sends nothing.

**Phase 8 — Private Property adapter.** Repeat Phase 4 for privateproperty.co.za. *Check:* same as Phase 4 and dedup test with a Property24 duplicate.

**Phase 9 — Scheduling + health.** systemd service + timer (below), run summary, degraded alerts, log rotation (`journald` is fine). *Check:* `systemctl list-timers` shows it; forced adapter break triggers a ⚠️ message.

```ini
# /etc/systemd/system/housebot.service
[Unit]
Description=Housebot daily run
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=housebot
WorkingDirectory=/home/housebot/housebot
EnvironmentFile=/home/housebot/housebot/.env
ExecStart=/home/housebot/housebot/.venv/bin/housebot run

# /etc/systemd/system/housebot.timer
[Unit]
Description=Run housebot daily
[Timer]
OnCalendar=*-*-* 07:00:00 Africa/Johannesburg
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
```

Use a systemd timer rather than PicoClaw's own cron feature, so the daily job never depends on the LLM or agent being up.

**Phase 10 — PicoClaw integration.**
1. Confirm PicoClaw runs with Telegram and an Ollama Cloud model (API key from ollama.com/settings/keys). Check the installed version's `config.example.json` for exact provider/channel keys — they change between releases.
2. Restrict PicoClaw's exec tool to the `housebot` binary if the version supports an allowlist; otherwise run PicoClaw as a low-privilege user whose only useful tool is `housebot`.
3. Create a skill `picoclaw/skills/housebot/SKILL.md` in PicoClaw's workspace skills folder:

```markdown
---
name: housebot
description: Query and manage the owner's Western Cape house-listing database. Use for anything about houses, listings, favourites, prices, or "#<number>" references.
---
Run commands with the exec tool. Always pass --json and summarise the result briefly.

- "fav 142" / "I like 142" → housebot fav add 142 [--note "..."]
- "hide 142" / "not interested in 142" → housebot hide 142
- "show my favourites" → housebot fav list --json
- "houses in Paarl under 3.5m with 4 beds" → housebot search --town Paarl --price-max 3500000 --beds-min 4 --json
- "tell me about 142" → housebot show 142 --json
- "what's new this week" → housebot search --since 7d --json
- "is the scraper working" → housebot sources --json
Never edit the database file directly. Never run housebot run unless the owner explicitly asks.
Always include the listing URL and #id in answers.
```

4. *Check:* on Telegram, "fav 142", "show my favourites", and "3-bed houses in Stellenbosch under R4m" all return correct results.

**Phase 11 — Email-alert adapter.** Implement §4.3. *Check:* a forwarded sample alert email in fixtures parses into listing IDs/URLs.

**Phase 12 — Hardening.** Nightly SQLite backup (`sqlite3 housebot.db ".backup data/backup-$(date +%F).db"`, keep 14), README with setup steps, `pytest` passes.

---

## 11. Acceptance criteria

- [ ] Daily run completes on the Pi in under 15 minutes with both portal adapters enabled.
- [ ] A matching listing is sent once; unchanged it is never sent again; a price change sends exactly one new message showing old → new price.
- [ ] The same house on two portals at the same price produces one message.
- [ ] Hidden listings never notify.
- [ ] A broken adapter produces a ⚠️ Telegram alert and doesn't stop other sources.
- [ ] Owner can favourite, hide, and search listings by chatting with PicoClaw.
- [ ] No secrets in the repo; request rate stays within §1 limits.

## 12. Future ideas (not in v1)

Weekly digest of favourites with price movements; commute-time filter via a maps API; load-shedding/solar keyword scoring; LLM-written one-line summary per listing (generated once at insert time, stored in DB).