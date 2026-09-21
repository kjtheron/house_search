"""SQLite storage: schema migrations, listing upserts, notifications and owner actions.

The "never notify the same house at the same price twice" rule (plan §6.4) lives in
pending_notifications(), so it is enforced by the database and survives restarts.
Migrations are the numbered .sql files in housebot/migrations/, tracked by PRAGMA user_version.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from dataclasses import fields

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

def upsert(conn, l: Listing, matches: bool, fp: str, ts: str | None = None) -> str:
    """Insert or update one listing. Returns new | price_change | relisted | unchanged."""
    ts = ts or now()
    row = l.as_row() | {"fingerprint": fp, "matches": int(matches),
                        "raw_json": json.dumps(l.raw) if l.raw else None}
    with conn:
        old = conn.execute("SELECT id, price, status FROM listings WHERE source=? AND source_listing_id=?",
                           (l.source, l.source_listing_id)).fetchone()
        if old is None:
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


def pending_notifications(conn) -> list[dict]:
    """Matching, active, not hidden, and this house not yet notified at this price (plan §6.4)."""
    rows = conn.execute("""
        SELECT l.*,
          (SELECT price_notified FROM notifications n WHERE n.fingerprint = l.fingerprint
           ORDER BY n.id DESC LIMIT 1) AS last_price,
          EXISTS (SELECT 1 FROM notifications n WHERE n.fingerprint = l.fingerprint) AS notified_before
        FROM listings l
        WHERE l.matches = 1 AND l.status = 'active'
          AND l.fingerprint NOT IN (SELECT h2.fingerprint FROM hidden h JOIN listings h2 ON h2.id = h.listing_id)
          AND NOT EXISTS (SELECT 1 FROM notifications n
                          WHERE n.fingerprint = l.fingerprint AND n.price_notified IS l.price)
        ORDER BY l.first_seen, l.id""").fetchall()
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
    names = [f.name for f in fields(Listing) if f.name != "raw"]
    changed = 0
    with conn:
        for r in conn.execute(f"SELECT id, matches, {', '.join(names)} FROM listings").fetchall():
            m = int(matches_fn(Listing(**{n: r[n] for n in names})))
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


def set_status(conn, listing_id: int, status: str) -> None:
    with conn:
        conn.execute("UPDATE listings SET status=?, last_checked=? WHERE id=?", (status, now(), listing_id))


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
                "f.rating, f.note, h.listing_id IS NOT NULL AS hidden")


def search(conn, town=None, suburb=None, price_max=None, beds_min=None, since=None, text=None,
           favs=False, include_gone=False, matching_only=False, limit=20) -> list[dict]:
    where, args = [], []
    for sql, val in (("l.town LIKE ?", town), ("l.suburb LIKE ?", suburb),
                     ("l.price <= ?", price_max), ("l.beds >= ?", beds_min), ("l.first_seen >= ?", since)):
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
    sql = (f"SELECT {SUMMARY_COLS} FROM listings l LEFT JOIN favourites f ON f.listing_id = l.id "
           f"LEFT JOIN hidden h ON h.listing_id = l.id "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY l.first_seen DESC, l.id DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, [*args, limit])]


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
