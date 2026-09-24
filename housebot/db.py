"""SQLite storage: schema migrations, listing upserts, notifications and owner actions.

The "never notify the same house at the same price twice" rule (plan §6.4) lives in
pending_notifications(), so it is enforced by the database and survives restarts.
Migrations are the numbered .sql files in housebot/migrations/, tracked by PRAGMA user_version.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from importlib import resources
from pathlib import Path

from .match import _key as _place_key
from .models import Listing

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(resources.files("housebot.migrations").iterdir(), key=lambda f: f.name)
    for f in files:
        if not f.name.endswith(".sql"):
            continue
        n = int(f.name.split("_")[0])
        if n > version:
            with conn:
                conn.executescript(f.read_text())
                conn.execute(f"PRAGMA user_version = {n}")


# --- pipeline ---------------------------------------------------------------

def own_key(l: Listing) -> str:
    return f"{l.source}:{l.source_listing_id}"


def upsert(conn, l: Listing, matches: bool, ts: str | None = None) -> str:
    """Insert or update one listing. Returns new | price_change | relisted | unchanged.

    `fingerprint` groups the same house across sites: a new listing joins its twin on the other
    site (same_house) or gets its own key. It is never changed by later updates.
    """
    ts = ts or now()
    row = l.as_row() | {"matches": int(matches),
                        "raw_json": json.dumps(l.raw) if l.raw else None}
    with conn:
        old = conn.execute("SELECT id, price, status FROM listings WHERE source=? AND source_listing_id=?",
                           (l.source, l.source_listing_id)).fetchone()
        if old is None:
            row["fingerprint"] = same_house(conn, l) or own_key(l)
            cols = list(row) + ["first_seen", "last_seen"]
            cur = conn.execute(f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [*row.values(), ts, ts])
            conn.execute("INSERT INTO price_history VALUES (?,?,?)", (cur.lastrowid, l.price, ts))
            return "new"
        # Keep known values if this pass didn't see them (e.g. card vs. detail page).
        updates = {k: v for k, v in row.items() if v is not None or k in ("price", "matches")}
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE listings SET {sets}, last_seen=? WHERE id=?",
                     [*updates.values(), ts, old["id"]])
        if old["price"] != l.price:
            conn.execute("INSERT INTO price_history VALUES (?,?,?)", (old["id"], l.price, ts))
            return "price_change"
        return "relisted" if old["status"] in ("gone", "sold") and l.status == "active" else "unchanged"


def _similar_place(a: str | None, b: str | None) -> bool:
    """Sites spell places differently: "Bot River" ~ "Botrivier", "Sir Lowry's Pass" ~ "Sir Lowrys Pass"."""
    a, b = _place_key(a or ""), _place_key(b or "")
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.85)


def same_house(conn, l: Listing, before_id: int | None = None) -> str | None:
    """Fingerprint of this house as already listed elsewhere, if any.

    Another site: same type, beds and baths, a similar suburb name, plus the same price, the same monthly
    rates, or an erf/floor size within 2%. Catches pairs the size-based fingerprint misses (a card without
    sizes, one site giving only floor size, different spellings). Garages are ignored: sites count parking differently.
    Same site (one house, several agencies): all of that plus the same price, a size within 2% and a
    different agency, so units in one development (same agency) stay apart.
    before_id: only consider listings stored before this one (used by relink()).
    """
    if not (l.suburb and l.beds is not None):
        return None
    before = before_id or 2**62
    lo_hi = lambda x: (x * 0.98, x / 0.98) if x else (None, None)
    # Candidates come from the price/erf/floor indexes, never a full scan.
    rows = conn.execute(
        "SELECT fingerprint, suburb FROM listings l WHERE l.id IN ("
        " SELECT id FROM listings WHERE price = ?"
        " UNION SELECT id FROM listings WHERE rates = ?"
        " UNION SELECT id FROM listings WHERE erf_m2 BETWEEN ? AND ?"
        " UNION SELECT id FROM listings WHERE floor_m2 BETWEEN ? AND ?) "
        "AND l.id < ? AND l.beds = ? AND l.baths IS ? AND l.property_type IS ? AND ("
        # another site: a group that already has a listing from this site has its twin, never a 2nd one
        " (l.source <> ? AND NOT EXISTS (SELECT 1 FROM listings x WHERE x.fingerprint = l.fingerprint"
        "  AND x.source = ? AND x.id < ?))"
        # same site, an agency the group doesn't have yet (NULL agency or price never passes)
        " OR (l.source = ? AND l.price = ? AND l.agency <> ?"
        "  AND (l.erf_m2 BETWEEN ? AND ? OR l.floor_m2 BETWEEN ? AND ?)"
        "  AND NOT EXISTS (SELECT 1 FROM listings x WHERE x.fingerprint = l.fingerprint AND x.agency = ?"
        "   AND x.id < ?))) "
        "ORDER BY l.id",
        (l.price, l.rates, *lo_hi(l.erf_m2), *lo_hi(l.floor_m2), before, l.beds, l.baths, l.property_type,
         l.source, l.source, before, l.source, l.price, l.agency, *lo_hi(l.erf_m2), *lo_hi(l.floor_m2),
         l.agency, before)).fetchall()
    return next((r["fingerprint"] for r in rows if _similar_place(r["suburb"], l.suburb)), None)


HASH_FP = re.compile(r"[0-9a-f]{40}")  # old size-hash keys, which could merge two houses on one site


def needs_relink(conn) -> bool:
    """True while old size-hash keys are stored (new listings are linked when inserted)."""
    return conn.execute("SELECT 1 FROM listings WHERE length(fingerprint) = 40 "
                        "AND fingerprint NOT GLOB '*[^0-9a-f]*' LIMIT 1").fetchone() is not None


def relink(conn) -> int:
    """Join stored listings to the same house on the other site. Only ever merges, so a link
    survives a later price change. Returns listings changed. No network."""
    changed = 0
    with conn:
        for r in conn.execute("SELECT * FROM listings ORDER BY id").fetchall():
            l = Listing.from_row(r)
            current = own_key(l) if HASH_FP.fullmatch(r["fingerprint"] or "") else r["fingerprint"]
            linked = current != own_key(l)
            fp = current if linked else (same_house(conn, l, before_id=r["id"]) or current)
            if fp != r["fingerprint"]:
                conn.execute("UPDATE listings SET fingerprint=? WHERE id=?", (fp, r["id"]))
                changed += 1
        if changed:  # notifications follow their listing's group
            conn.execute("UPDATE notifications SET fingerprint = "
                         "(SELECT fingerprint FROM listings WHERE id = notifications.listing_id)")
    return changed


def start_run(conn, source: str) -> int:
    with conn:
        return conn.execute("INSERT INTO runs (source, started_at) VALUES (?,?)", (source, now())).lastrowid


def finish_run(conn, run_id: int, status: str, seen=0, new=0, price_changes=0, errors=0) -> None:
    with conn:
        conn.execute("UPDATE runs SET finished_at=?, status=?, seen=?, new=?, price_changes=?, errors=? "
                     "WHERE id=?", (now(), status, seen, new, price_changes, errors, run_id))


def mark_gone(conn, source: str, runs: int = 3) -> int:
    """After an ok run: listings not seen in the last `runs` ok runs are gone."""
    row = conn.execute("SELECT started_at FROM runs WHERE source=? AND status='ok' "
                       "ORDER BY id DESC LIMIT 1 OFFSET ?", (source, runs - 1)).fetchone()
    if not row:
        return 0
    with conn:
        return conn.execute("UPDATE listings SET status='gone' WHERE source=? AND status='active' "
                            "AND last_seen < ?", (source, row[0])).rowcount


DETAIL_COLS = ("floor_m2", "erf_m2", "garages", "parking", "storeys", "ensuites", "rates", "levies", "pets",
               "features", "listed_at", "description", "status")


def pending_details(conn, max_attempts: int, limit: int = -1) -> list[dict]:
    """Matching listings still waiting for their listing page, oldest first (limit -1 = all)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM listings WHERE matches = 1 AND status IN ('active', 'under_offer') "
        "AND detail_fetched_at IS NULL AND detail_attempts < ? "
        "AND id NOT IN (SELECT listing_id FROM hidden) ORDER BY first_seen, id LIMIT ?", (max_attempts, limit))]


def detailed_mate(conn, listing: dict) -> dict | None:
    """The same house on another site, if its details are already fetched (no request needed)."""
    r = conn.execute(f"SELECT {', '.join(DETAIL_COLS)} FROM listings WHERE fingerprint = ? AND id <> ? "
                     "AND detail_fetched_at IS NOT NULL LIMIT 1", (listing["fingerprint"], listing["id"])).fetchone()
    return dict(r) if r else None


def apply_details(conn, listing_id: int, d: dict, matches_fn) -> bool:
    """Store fetched details (keeping card values the page lacks) and re-match. Returns new match flag."""
    d = {k: v for k, v in d.items() if k in DETAIL_COLS and v is not None}
    if isinstance(d.get("features"), list):
        d["features"] = json.dumps(d["features"])
    if "pets" in d:
        d["pets"] = int(d["pets"])
    with conn:
        sets = "".join(f"{k}=?, " for k in d)
        conn.execute(f"UPDATE listings SET {sets}detail_fetched_at=?, last_checked=? WHERE id=?",
                     [*d.values(), now(), now(), listing_id])
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        l = Listing.from_row(row)
        m = int(matches_fn(l))
        conn.execute("UPDATE listings SET matches=? WHERE id=?", (m, listing_id))
        # Not linked yet? Sizes and rates from the page may now reveal the twin on the other site.
        if row["fingerprint"] == own_key(l):
            fp = same_house(conn, l)
            if fp:
                conn.execute("UPDATE listings SET fingerprint=? WHERE id=?", (fp, listing_id))
                conn.execute("UPDATE notifications SET fingerprint=? WHERE listing_id=?", (fp, listing_id))
    return bool(m)


def detail_failed(conn, listing_id: int) -> None:
    with conn:
        conn.execute("UPDATE listings SET detail_attempts = detail_attempts + 1 WHERE id=?", (listing_id,))


def details_waiting(conn, max_attempts: int) -> int:
    return conn.execute("SELECT count(*) FROM listings WHERE matches = 1 AND status IN ('active', 'under_offer') "
                        "AND detail_fetched_at IS NULL AND detail_attempts < ?", (max_attempts,)).fetchone()[0]


def pending_notifications(conn, hold_for_details: int | None = None) -> list[dict]:
    """Matching, active, not hidden, and this house not yet notified at this price (plan §6.4).

    hold_for_details=N: skip listings still waiting for their details (unless N attempts failed).
    """
    rows = conn.execute("""
        SELECT l.*,
          (SELECT price_notified FROM notifications n WHERE n.fingerprint = l.fingerprint
           ORDER BY n.id DESC LIMIT 1) AS last_price,
          EXISTS (SELECT 1 FROM notifications n WHERE n.fingerprint = l.fingerprint) AS notified_before
        FROM listings l
        WHERE l.matches = 1 AND l.status = 'active'
          AND (? IS NULL OR l.detail_fetched_at IS NOT NULL OR l.detail_attempts >= ?)
          AND l.fingerprint NOT IN (SELECT h2.fingerprint FROM hidden h JOIN listings h2 ON h2.id = h.listing_id)
          AND NOT EXISTS (SELECT 1 FROM notifications n
                          WHERE n.fingerprint = l.fingerprint AND n.price_notified IS l.price)
        ORDER BY l.first_seen, l.id""", (hold_for_details, hold_for_details)).fetchall()
    out, seen_fp = [], set()
    for r in rows:
        if r["fingerprint"] in seen_fp:  # same house on another portal in this batch
            continue
        seen_fp.add(r["fingerprint"])
        out.append(dict(r) | {"reason": "price_change" if r["notified_before"] else "new"})
    return out


def record_notification(conn, listing: dict, msg_id: int | None) -> None:
    with conn:
        conn.execute("INSERT INTO notifications (listing_id, fingerprint, price_notified, reason, sent_at, "
                     "telegram_msg_id) VALUES (?,?,?,?,?,?)",
                     (listing["id"], listing["fingerprint"], listing["price"], listing["reason"], now(), msg_id))


def rematch(conn, matches_fn) -> int:
    """Re-run matching on every stored listing (after the search config changed). Returns rows changed."""
    changed = 0
    with conn:
        for r in conn.execute("SELECT * FROM listings").fetchall():
            m = int(matches_fn(Listing.from_row(r)))
            if m != r["matches"]:
                conn.execute("UPDATE listings SET matches=? WHERE id=?", (m, r["id"]))
                changed += 1
    return changed


def delete_town(conn, town: str) -> tuple[int, int]:
    """Delete a town's listings and their history, except favourites. Returns (deleted, kept)."""
    where = "lower(town) = lower(?) AND id NOT IN (SELECT listing_id FROM favourites)"
    with conn:
        ids = f"SELECT id FROM listings WHERE {where}"
        for table in ("price_history", "notifications", "hidden"):
            conn.execute(f"DELETE FROM {table} WHERE listing_id IN ({ids})", (town,))
        deleted = conn.execute(f"DELETE FROM listings WHERE {where}", (town,)).rowcount
    kept = conn.execute("SELECT count(*) FROM listings WHERE lower(town) = lower(?)", (town,)).fetchone()[0]
    return deleted, kept


def to_check(conn, ids=None, favs=False, limit=20) -> list[dict]:
    """Listings to re-check: given IDs, or favourites, or matching listings checked longest ago."""
    if ids:
        sql, args = f"SELECT * FROM listings WHERE id IN ({','.join('?' * len(ids))})", list(ids)
    else:
        cond = ("id IN (SELECT listing_id FROM favourites)" if favs else
                "(matches = 1 OR id IN (SELECT listing_id FROM favourites))")
        # A listing seen on a search page in the last day already showed its status there.
        sql = (f"SELECT * FROM listings WHERE {cond} AND status IN ('active', 'under_offer') "
               "AND coalesce(last_checked, last_seen) < ? ORDER BY coalesce(last_checked, last_seen) LIMIT ?")
        args = [(datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"), limit]
    return [dict(r) for r in conn.execute(sql, args)]


def town_names(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT town FROM listings WHERE town IS NOT NULL ORDER BY town")]


# --- owner actions ----------------------------------------------------------

def exists(conn, listing_id: int) -> bool:
    return conn.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,)).fetchone() is not None


def fav_add(conn, listing_id: int, rating: int | None = None, note: str | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO favourites VALUES (?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET "
                     "rating=coalesce(excluded.rating, rating), note=coalesce(excluded.note, note)",
                     (listing_id, rating, note, now()))


def fav_rm(conn, listing_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM favourites WHERE listing_id=?", (listing_id,)).rowcount > 0


def hide(conn, listing_id: int, reason: str | None = None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO hidden VALUES (?,?,?)", (listing_id, reason, now()))


def unhide(conn, listing_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM hidden WHERE listing_id=?", (listing_id,)).rowcount > 0


# --- queries ----------------------------------------------------------------

SUMMARY_COLS = ("l.id, l.source, l.url, l.title, l.town, l.suburb, l.property_type, l.price, l.beds, "
                "l.baths, l.garages, l.floor_m2, l.erf_m2, l.status, l.first_seen, l.last_seen, "
                "l.fingerprint, f.rating, f.note, l.fingerprint IN (SELECT fingerprint FROM hidden_fps) AS hidden")


def search(conn, town=None, suburb=None, price_max=None, beds_min=None, since=None, text=None,
           favs=False, include_gone=False, matching_only=False, detailed=False, limit=20,
           include_hidden=False, source=None) -> list[dict]:
    where, args = [], []
    for sql, val in (("l.town LIKE ?", town), ("l.suburb LIKE ?", suburb), ("l.source = ?", source),
                     ("l.price <= ?", price_max), ("l.beds >= ?", beds_min), ("g.first_seen >= ?", since)):
        if val is not None:
            where.append(sql)
            args.append(val)
    if text:
        where.append("(l.title LIKE ? OR l.description LIKE ?)")
        args += [f"%{text}%"] * 2
    if favs:
        where.append("f.listing_id IS NOT NULL")
    if not include_gone:
        where.append("l.status IN ('active', 'under_offer')")
    if matching_only:
        where.append("l.matches = 1")
    if not include_hidden:  # hiding one site's copy hides the house, as for notifications
        where.append("l.fingerprint NOT IN (SELECT fingerprint FROM hidden_fps)")
    if detailed:
        where.append("l.detail_fetched_at IS NOT NULL")
    # A house is as new as its first copy on any site, and shows under its oldest listing number, so a
    # copy found later on another site doesn't make it look new.
    sql = (f"WITH hidden_fps AS (SELECT l2.fingerprint FROM hidden h JOIN listings l2 ON l2.id = h.listing_id), "
           f"g AS (SELECT fingerprint, MIN(first_seen) AS first_seen FROM listings GROUP BY fingerprint) "
           f"SELECT {SUMMARY_COLS} FROM listings l JOIN g ON g.fingerprint = l.fingerprint "
           f"LEFT JOIN favourites f ON f.listing_id = l.id "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY g.first_seen DESC, l.fingerprint, l.id")
    # One row per house: the same house on another site is folded into `also_on`.
    out, by_fp = [], {}
    for r in map(dict, conn.execute(sql, args)):
        if r["fingerprint"] in by_fp:
            by_fp[r["fingerprint"]]["also_on"].append(r["source"])
            continue
        if len(out) < limit:
            by_fp[r["fingerprint"]] = r | {"also_on": []}
            out.append(by_fp[r["fingerprint"]])
    return out


def show(conn, listing_id: int) -> dict | None:
    row = conn.execute("SELECT l.*, f.rating, f.note, h.reason AS hidden_reason FROM listings l "
                       "LEFT JOIN favourites f ON f.listing_id=l.id LEFT JOIN hidden h ON h.listing_id=l.id "
                       "WHERE l.id=?", (listing_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("raw_json")
    d["price_history"] = [dict(r) for r in conn.execute(
        "SELECT price, seen_at FROM price_history WHERE listing_id=? ORDER BY seen_at", (listing_id,))]
    d["notifications"] = [dict(r) for r in conn.execute(
        "SELECT reason, price_notified, sent_at FROM notifications WHERE fingerprint=? ORDER BY id",
        (d["fingerprint"],))]
    d["same_house"] = [dict(r) for r in conn.execute(
        "SELECT id, source, url, price FROM listings WHERE fingerprint=? AND id<>?", (d["fingerprint"], listing_id))]
    return d


def last_runs(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE id IN (SELECT max(id) FROM runs GROUP BY source) ORDER BY source")]
