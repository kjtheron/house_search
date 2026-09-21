"""The daily job that `housebot run` does.

collect(): run each enabled adapter, match every listing, upsert it into the DB, and mark
the source ok / degraded / failed (saving the HTML of a broken page to data/debug/).
notify(): send pending alerts, degraded-source warnings, and a short run summary.
check(): open listing pages to confirm a house is still for sale (sold / under offer / gone).
backfill(): read every page of one town once, for its full history.
"""

import logging
import queue
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import db
from .adapters import ADAPTERS
from .config import Config
from .fingerprint import fingerprint
from .http import Blocked, PoliteClient
from .match import matches
from .notify import format_listing

log = logging.getLogger(__name__)
_DONE = object()  # end-of-source marker on the fetch queue


@contextmanager
def session(cfg: Config):
    """Open the DB and the polite HTTP client; always close both."""
    conn = db.connect(cfg.paths.db)
    http = PoliteClient(cfg.http)
    try:
        yield conn, http
    finally:
        http.close()
        conn.close()


def collect(cfg: Config, conn, http, source: str | None = None, adapters=ADAPTERS,
            debug_dir: Path = Path("data/debug"), backfill: bool = False) -> list[dict]:
    """Scrape every enabled source into the DB. Returns one result dict per source.

    Sources are fetched in parallel threads (each site keeps its own polite pace); all DB
    writes stay on this thread, because a SQLite connection must not cross threads.
    backfill=True reads every page (no early stop) and never marks listings gone,
    because it only looks at part of the province.
    """
    jobs = {}
    for name, src in cfg.sources.items():
        if not src.enabled or (source and name != source) or name not in adapters:
            continue
        known = set() if backfill else {
            r[0] for r in conn.execute("SELECT source_listing_id FROM listings WHERE source=?", (name,))}
        jobs[name] = {"adapter": adapters[name](http, src), "known": known, "last_html": "",
                      "run_id": db.start_run(conn, f"{name} (backfill)" if backfill else name),
                      "res": {"source": name, "status": "ok", "seen": 0, "new": 0, "price_changes": 0,
                              "errors": 0, "why": ""}}

    q: queue.Queue = queue.Queue()

    def fetch(name, job):
        try:
            for page in job["adapter"].pages(cfg.search, job["known"]):
                q.put((name, page, None))
        except Exception as e:
            q.put((name, None, e))
        q.put((name, None, _DONE))

    for name, job in jobs.items():
        threading.Thread(target=fetch, args=(name, job), daemon=True).start()
    running = len(jobs)
    while running:
        name, page, err = q.get()
        job, res = jobs[name], jobs[name]["res"]
        if err is _DONE:
            running -= 1
        elif isinstance(err, Blocked):
            res.update(status="degraded", why=f"blocked: {err}")
        elif err is not None:
            log.error("%s failed", name, exc_info=err)
            res.update(status="failed", why=f"{type(err).__name__}: {err}")
        else:
            job["last_html"] = page.html
            res["errors"] += page.errors
            for l in page.listings:
                res["seen"] += 1
                change = db.upsert(conn, l, matches(l, cfg.search), fingerprint(l))
                res["new"] += change == "new"
                res["price_changes"] += change == "price_change"

    results = []
    for name, job in jobs.items():
        res = job["res"]
        if res["status"] == "ok" and res["seen"] == 0:
            res.update(status="degraded", why="0 listings")
        elif res["status"] == "ok" and res["errors"] > res["seen"]:
            res.update(status="degraded", why=f"{res['errors']} parse errors")
        if res["status"] != "ok" and job["last_html"]:
            path = debug_dir / name / f"{date.today()}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(job["last_html"])
        db.finish_run(conn, job["run_id"], res["status"], res["seen"], res["new"], res["price_changes"],
                      res["errors"])
        # Only a full read of the results proves a listing is gone (not after an early stop).
        if res["status"] == "ok" and not backfill and job["adapter"].complete:
            res["gone"] = db.mark_gone(conn, name)
        results.append(res)
    return results


def notify(cfg: Config, conn, notifier, results: list[dict], record: bool = True) -> int:
    """Send pending matches, then alerts and a run summary. Returns listings sent."""
    pending = db.pending_notifications(conn)
    sent = 0
    for l in pending[: cfg.notify.max_per_run]:
        try:
            msg_id = notifier.send(format_listing(l), l["photo_url"] if cfg.notify.send_photo else None)
        except Exception as e:  # not recorded -> retried next run
            log.error("send #%s failed: %s", l["id"], e)
            continue
        if record:
            db.record_notification(conn, l, msg_id)
        sent += 1
    if len(pending) > cfg.notify.max_per_run:
        notifier.send(f"…and {len(pending) - cfg.notify.max_per_run} more. "
                      f"Ask me for them, or run <code>housebot search --since today</code>.")
    for r in results:
        if r["status"] != "ok":
            notifier.send(f"⚠️ {r['source']} adapter {r['status']} ({r['why']}). Site may have changed.")
    if sent or not cfg.notify.quiet_if_none:
        lines = [f"{r['source']}: {r['seen']} seen, {r['new']} new, {r['price_changes']} price changes"
                 for r in results]
        head = f"✅ Daily run: {sent} match{'es' * (sent != 1)} sent." if sent else "No new matches today."
        notifier.send("\n".join([head, *lines]))
    return sent


STATUS_WORDS = {"active": "🟢 for sale again", "under_offer": "🟡 UNDER OFFER", "sold": "🔴 SOLD",
                "gone": "⚫ no longer listed"}


def check(cfg: Config, conn, http, listings: list[dict], adapters=ADAPTERS) -> list[dict]:
    """Open each listing page and store its sale status. Returns the listings whose status changed."""
    changed = []
    sites = {name: adapters[name](http, src) for name, src in cfg.sources.items() if name in adapters}
    for l in listings:
        if l["source"] not in sites:
            continue
        try:
            status = sites[l["source"]].listing_status(l["url"])
        except Blocked as e:
            log.warning("%s checks stopped for this run: %s", l["source"], e)
            del sites[l["source"]]
            continue
        except Exception as e:
            log.warning("check #%s failed: %s", l["id"], e)
            continue
        db.set_status(conn, l["id"], status)
        if status != l["status"]:
            changed.append(l | {"old_status": l["status"], "status": status})
    return changed


def backfill(cfg: Config, conn, http, town_ids: dict, max_pages: int = 50) -> list[dict]:
    """Read every page for one town on each site ({source: (site town name, location ID)})."""
    sources = {name: cfg.sources[name].model_copy(update={"locations": {town: loc}, "max_pages": max_pages})
               for name, hit in town_ids.items() if hit for town, loc in [hit]}
    return collect(cfg.model_copy(update={"sources": sources}), conn, http, backfill=True)


def run(cfg: Config, notifier, source: str | None = None, record: bool = True) -> list[dict]:
    with session(cfg) as (conn, http):
        # Config may have changed since stored listings were matched; early stop means most of
        # them are never seen again, so re-match everything first (fast, no network).
        db.rematch(conn, lambda l: matches(l, cfg.search))
        results = collect(cfg, conn, http, source)
        notify(cfg, conn, notifier, results, record)
        if cfg.check.per_run:
            favs = {r[0] for r in conn.execute("SELECT listing_id FROM favourites")}
            todo = db.to_check(conn, favs=True, limit=cfg.check.per_run)
            todo += db.to_check(conn, limit=cfg.check.per_run - len(todo)) if len(todo) < cfg.check.per_run else []
            for l in check(cfg, conn, http, list({l["id"]: l for l in todo}.values())):
                if l["id"] in favs:
                    notifier.send(f"★ #{l['id']} {STATUS_WORDS[l['status']]}: {l['town'] or ''}, "
                                  f"{l['suburb'] or ''}\n{l['url']}")
        return results
