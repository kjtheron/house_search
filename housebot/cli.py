"""Command-line interface (`housebot ...`), built with typer.

This is the only interface to the bot. You use it by hand, systemd runs `housebot run`
daily, and PicoClaw calls the other commands (always with --json) when you chat on Telegram.
Only run, fav, hide/unhide, towns, backfill, check, details and rematch write to the database; towns also edits
the `towns:` line of config.yaml. No command accepts raw SQL.
"""

import json
import logging
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import config as config_mod
from . import db, pipeline
from . import towns as towns_mod
from .match import matches
from .models import feature_key
from .notify import Console, Telegram, rand

app = typer.Typer(help="Western Cape house-listing bot.", no_args_is_help=True, add_completion=False,
                  callback=lambda: logging.basicConfig(level=logging.INFO,
                                                       format="%(levelname)s %(name)s: %(message)s"))
fav_app = typer.Typer(help="Favourites.", no_args_is_help=True)
config_app = typer.Typer(help="Config.", no_args_is_help=True)
towns_app = typer.Typer(help="Which towns to alert for (search.towns in config.yaml).", no_args_is_help=True)
app.add_typer(fav_app, name="fav")
app.add_typer(towns_app, name="towns")
app.add_typer(config_app, name="config")

Json = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
Bg = Annotated[bool, typer.Option("--bg", help="Run in the background; Telegram message when done.")]
Done = Annotated[bool, typer.Option("--telegram-when-done", hidden=True)]


def _spawn(argv: list[str]) -> None:
    """Re-run this command detached (PicoClaw's exec would time out), logging to data/background.log."""
    log = Path(_cfg().paths.db).parent / "background.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        subprocess.Popen([sys.executable, "-m", "housebot.cli", *argv, "--telegram-when-done"],
                         stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)


def _started(what: str, as_json: bool) -> None:
    _out({"started": what, "background": True}, as_json,
         lambda _: typer.echo(f"Started {what} in the background. You'll get a Telegram message when it's done."))


def _telegram(text: str) -> None:
    try:
        Telegram().send(text)
    except Exception as e:
        logging.error("done message not sent: %s", e)


def _cfg() -> config_mod.Config:
    try:
        return config_mod.load()
    except config_mod.ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)


def _conn():
    return db.connect(_cfg().paths.db)


def _out(data, as_json: bool, human) -> None:
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, default=str))
    else:
        human(data)


def _need(conn, listing_id: int) -> None:
    if not db.exists(conn, listing_id):
        typer.echo(f"No listing #{listing_id}", err=True)
        raise typer.Exit(1)


def _since(value: str | None) -> str | None:
    """'7d' | 'today' | '2026-09-01' -> ISO timestamp."""
    if value is None:
        return None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "today":
        return today.isoformat()
    if m := re.fullmatch(r"(\d+)d", value):
        return (today - timedelta(days=int(m[1]))).isoformat()
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        raise typer.BadParameter("use 7d, today or YYYY-MM-DD", param_hint="--since")


def _line(l: dict) -> str:
    beds = "/".join(f"{l[k]:g}{s}" for k, s in (("beds", "bd"), ("baths", "ba")) if l.get(k) is not None)
    size = f"erf {l['erf_m2']}m²" if l.get("erf_m2") else ""
    flags = ("★" if l.get("rating") is not None or l.get("note") is not None else "") + ("✗" if l.get("hidden") else "")
    if l.get("status") not in (None, "active"):
        flags += f" [{l['status'].replace('_', ' ').upper()}]"
    where = ", ".join(x for x in (l.get("town"), l.get("suburb")) if x)
    also = f"  (+ {', '.join(dict.fromkeys(l['also_on']))})" if l.get("also_on") else ""
    return f"#{l['id']:<5} {rand(l['price']):>13}  {where}  {beds} {size} {flags}{also}\n       {l['url']}"


@app.command()
def run(source: Annotated[Optional[str], typer.Option(help="Only this source.")] = None,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Print messages; no Telegram, no notification rows.")] = False):
    """Full pipeline: scrape, match, store, notify."""
    cfg = _cfg()
    notifier = Console() if dry_run else Telegram()
    results = pipeline.run(cfg, notifier, source, record=not dry_run)
    if any(r["status"] == "failed" for r in results):
        raise typer.Exit(1)


@app.command()
def search(town: Optional[str] = None, suburb: Optional[str] = None,
           price_max: Optional[int] = None, beds_min: Optional[float] = None,
           since: Annotated[Optional[str], typer.Option(help="7d, today or YYYY-MM-DD (first seen).")] = None,
           text: Annotated[Optional[str], typer.Option(help="Word in title or description.")] = None,
           favs: bool = False, include_gone: bool = False, include_hidden: bool = False,
           matching: Annotated[bool, typer.Option(help="Only listings that pass config.yaml.")] = False,
           detailed: Annotated[bool, typer.Option(help="Only listings whose own page was fetched.")] = False,
           source: Annotated[Optional[str], typer.Option(help="Only this site, e.g. remax.")] = None,
           limit: int = 20, as_json: Json = False):
    """Query stored listings."""
    rows = db.search(_conn(), town, suburb, price_max, beds_min, _since(since), text, favs, include_gone,
                     matching, detailed, limit, include_hidden, source)
    _out(rows, as_json, lambda rs: typer.echo("\n".join(map(_line, rs)) or "No listings."))


@app.command()
def show(listing_id: int, as_json: Json = False):
    """Full detail + price and notification history."""
    conn = _conn()
    _need(conn, listing_id)
    d = db.show(conn, listing_id)

    def human(d):
        skip = {"price_history", "notifications", "same_house"}
        for k, v in d.items():
            if k not in skip and v is not None:
                typer.echo(f"{k:>18}: {v}")
        typer.echo("     price history: " + ", ".join(f"{rand(p['price'])} ({p['seen_at'][:10]})" for p in d["price_history"]))
        for n in d["notifications"]:
            typer.echo(f"          notified: {n['reason']} at {rand(n['price_notified'])} on {n['sent_at'][:10]}")
        for s in d["same_house"]:
            typer.echo(f"        same house: #{s['id']} {s['source']} {rand(s['price'])} {s['url']}")
    _out(d, as_json, human)


@fav_app.command("add")
def fav_add(listing_id: int, rating: Annotated[Optional[int], typer.Option(min=1, max=5)] = None,
            note: Optional[str] = None, as_json: Json = False):
    conn = _conn()
    _need(conn, listing_id)
    db.fav_add(conn, listing_id, rating, note)
    _out({"ok": True, "id": listing_id}, as_json, lambda _: typer.echo(f"★ #{listing_id} added to favourites"))


@fav_app.command("rm")
def fav_rm(listing_id: int, as_json: Json = False):
    ok = db.fav_rm(_conn(), listing_id)
    _out({"ok": ok, "id": listing_id}, as_json,
         lambda _: typer.echo(f"#{listing_id} removed" if ok else f"#{listing_id} was not a favourite"))


@fav_app.command("list")
def fav_list(as_json: Json = False):
    rows = db.search(_conn(), favs=True, include_gone=True, limit=1000, include_hidden=True)
    _out(rows, as_json, lambda rs: typer.echo("\n".join(map(_line, rs)) or "No favourites yet."))


@app.command()
def hide(listing_id: int, reason: Optional[str] = None, as_json: Json = False):
    """Never notify about this house again."""
    conn = _conn()
    _need(conn, listing_id)
    db.hide(conn, listing_id, reason)
    _out({"ok": True, "id": listing_id}, as_json, lambda _: typer.echo(f"#{listing_id} hidden"))


@app.command()
def unhide(listing_id: int, as_json: Json = False):
    ok = db.unhide(_conn(), listing_id)
    _out({"ok": ok, "id": listing_id}, as_json,
         lambda _: typer.echo(f"#{listing_id} unhidden" if ok else f"#{listing_id} was not hidden"))


@app.command()
def stats(as_json: Json = False):
    """Counts per town, median price, new this week, last run status."""
    conn = _conn()
    towns = [dict(r) for r in conn.execute(
        "SELECT town, count(*) AS active, sum(matches) AS matching FROM listings "
        "WHERE status='active' GROUP BY town ORDER BY town")]
    prices = [r[0] for r in conn.execute(
        "SELECT price FROM listings WHERE status='active' AND matches=1 AND price IS NOT NULL")]
    week = conn.execute("SELECT count(*) FROM listings WHERE matches=1 AND first_seen >= ?",
                        (_since("7d"),)).fetchone()[0]
    d = {"towns": towns, "median_matching_price": statistics.median(prices) if prices else None,
         "new_matches_7d": week, "favourites": conn.execute("SELECT count(*) FROM favourites").fetchone()[0],
         "last_runs": db.last_runs(conn)}

    def human(d):
        for t in d["towns"]:
            typer.echo(f"{t['town'] or '?':<16} {t['active']:>5} active {t['matching']:>5} matching")
        med = d["median_matching_price"]
        typer.echo(f"Median matching price: {rand(int(med)) if med else '-'}")
        typer.echo(f"New matches this week: {d['new_matches_7d']}   Favourites: {d['favourites']}")
        sources_human(d["last_runs"])
    _out(d, as_json, human)


def sources_human(runs):
    for r in runs:
        typer.echo(f"{r['source']:<16} {r['status'] or 'running':<9} {r['started_at']}  "
                   f"seen {r['seen']}, new {r['new']}, errors {r['errors']}")
    if not runs:
        typer.echo("No runs yet.")


@app.command()
def sources(as_json: Json = False):
    """Last run status per source."""
    _out(db.last_runs(_conn()), as_json, sources_human)


@config_app.command("show")
def config_show(as_json: Json = False):
    """Print the active config."""
    d = _cfg().model_dump(mode="json")
    _out(d, as_json, lambda d: typer.echo(json.dumps(d, indent=2, ensure_ascii=False)))


# --- towns ------------------------------------------------------------------

def _town_result(cfg, extra: dict) -> dict:
    return {"towns": cfg.search.towns, "any_town": not cfg.search.towns} | extra


@towns_app.command("list")
def towns_list(as_json: Json = False):
    """Show the town filter. Empty = alerts for any town in the province."""
    cfg = _cfg()
    _out(_town_result(cfg, {}), as_json,
         lambda d: typer.echo(", ".join(d["towns"]) if d["towns"] else "No town filter: alerting for any town."))


@towns_app.command("add")
def towns_add(town: str,
              history: Annotated[bool, typer.Option("--history", help="Also read every page of this town once "
                                                    "(full history; takes a few minutes).")] = False,
              bg: Bg = False, done: Done = False, as_json: Json = False):
    """Add a town. Existing listings there start matching and are alerted on the next run."""
    if bg:  # the whole command: even the first town-ID lookup can take a minute
        _spawn(["towns", "add", town, *(["--history"] if history else [])])
        return _started(f"adding {town}" + (" with full history" if history else ""), as_json)
    cfg = _cfg()
    town_mode = {n: src for n, src in cfg.sources.items() if src.enabled and src.locations}
    with pipeline.session(cfg) as (conn, http):
        known = db.town_names(conn)
        name = towns_mod.resolve(town, known)
        need_ids = [n for n, src in town_mode.items() if not towns_mod.resolve(name or town, list(src.locations))]
        ids = None
        if not name or history or need_ids:  # ask the sites (cached after the first time)
            ids = towns_mod.town_ids(cfg, http, town)
            name = name or next((hit[0] for hit in ids.values() if hit), None)

        def fail(msg):
            if done:
                _telegram("⚠️ " + msg)
            typer.echo(msg, err=True)
            raise typer.Exit(1)

        if not name:
            hint = towns_mod.suggestions(town, known + towns_mod.all_site_towns(cfg))
            fail(f"Unknown town '{town}'." + (f" Did you mean: {', '.join(hint)}?" if hint else ""))
        # Sources that search town by town need this town's ID on their site.
        missing = [n for n in need_ids if not ids.get(n)]
        if missing:
            fail(f"{name} not found on {', '.join(missing)}, so it can't be added to their town list.")
        locations = {n: town_mode[n].locations | {name: ids[n][1]} for n in need_ids}
        towns = cfg.search.towns + ([name] if towns_mod.resolve(name, cfg.search.towns) is None else [])
        if locations or towns != cfg.search.towns:
            towns_mod.update_config(towns=towns, locations=locations)
            cfg = _cfg()
        changed = db.rematch(conn, lambda l: matches(l, cfg.search))
        results = pipeline.backfill(cfg, conn, http, ids) if history else []
    text = (f"Added {name}. Towns: {', '.join(cfg.search.towns)}. {changed} stored listings changed match."
            + "".join(f"\n  {n} location ID: {locs[name]}" for n, locs in locations.items())
            + "".join(f"\n  {r['source']}: {r['seen']} seen, {r['new']} new ({r['status']})" for r in results))
    if done:
        _telegram("✅ " + text + ("\nNew matches come in the next daily run." if history else ""))
    _out(_town_result(cfg, {"added": name, "rematched": changed, "locations": locations, "backfill": results}),
         as_json, lambda d: typer.echo(text))


@towns_app.command("rm")
def towns_rm(town: str, as_json: Json = False):
    """Remove a town and delete its listings from the database (favourites are kept)."""
    cfg = _cfg()
    name = towns_mod.resolve(town, cfg.search.towns)
    if not name:
        typer.echo(f"'{town}' is not in the town list: {', '.join(cfg.search.towns) or '(empty)'}", err=True)
        raise typer.Exit(1)
    locations = {}
    for n, src in cfg.sources.items():
        key = towns_mod.resolve(name, list(src.locations))
        if key:
            locations[n] = {t: i for t, i in src.locations.items() if t != key}
    towns_mod.update_config(towns=[t for t in cfg.search.towns if t != name], locations=locations)
    cfg = _cfg()
    conn = db.connect(cfg.paths.db)
    deleted, kept = db.delete_town(conn, name)
    changed = db.rematch(conn, lambda l: matches(l, cfg.search))
    warn = "".join(f" {n} has no towns left, so it now searches the WHOLE province."
                   for n, locs in locations.items() if not locs)
    if not cfg.search.towns:
        warn += " Town list is now EMPTY: the next run alerts for ANY town in the province."
    _out(_town_result(cfg, {"removed": name, "deleted": deleted, "kept_favourites": kept, "rematched": changed,
                            "warning": warn.strip() or None}), as_json,
         lambda d: typer.echo(f"Removed {name}: deleted {deleted} listings, kept {kept} favourites.{warn}"))


@app.command()
def backfill(town: Annotated[Optional[str], typer.Argument(help="One town's full history; leave out to go "
                                                          "back further in each site's normal search.")] = None,
             pages: Annotated[int, typer.Option(help="Pages per property type per site (~20 listings each).",
                                                min=1, max=100)] = 30,
             source: Annotated[Optional[list[str]], typer.Option(help="Only this site (repeat for more).")] = None,
             bg: Bg = False, done: Done = False, as_json: Json = False):
    """Read older listings once, with no early stop: one town, or (no town) the normal search deeper."""
    what = f"full-history read of {town}" if town else f"backfill ({pages} pages per type)"
    if bg:
        _spawn(["backfill", *([town] if town else []), "--pages", str(pages),
                *[a for s in source or [] for a in ("--source", s)]])
        return _started(what, as_json)
    cfg = _cfg()
    if source:
        unknown = set(source) - set(cfg.sources)
        if unknown:
            typer.echo(f"Unknown source: {', '.join(sorted(unknown))}. Sources: {', '.join(cfg.sources)}", err=True)
            raise typer.Exit(1)
        cfg = cfg.model_copy(update={"sources": {n: s for n, s in cfg.sources.items() if n in source}})
    with pipeline.session(cfg) as (conn, http):
        ids = None
        if town:
            ids = towns_mod.town_ids(cfg, http, town)
            if not any(ids.values()):
                if done:
                    _telegram(f"⚠️ No site knows a town called '{town}'.")
                typer.echo(f"No site knows a town called '{town}'.", err=True)
                raise typer.Exit(1)
        results = pipeline.backfill(cfg, conn, http, ids, max_pages=pages)
    summary = "\n".join(f"{r['source']}: {r['seen']} seen, {r['new']} new, {r['status']}" for r in results)
    if done:
        _telegram(f"✅ {what[0].upper() + what[1:]} done.\n{summary}\nNew matches come in the next daily run.")
    _out(results, as_json, lambda rs: typer.echo(summary))


@app.command()
def check(listing_ids: Annotated[Optional[list[int]], typer.Argument(help="Listing numbers; default: favourites "
                                                                     "and matches checked longest ago.")] = None,
          favs: bool = False, limit: int = 20, bg: Bg = False, done: Done = False, as_json: Json = False):
    """Open listing pages to confirm each house is still for sale (sold / under offer / gone)."""
    if bg:
        argv = ["check", *map(str, listing_ids or []), *(["--favs"] if favs else []), "--limit", str(limit)]
        _spawn(argv)
        return _started("availability check", as_json)
    cfg = _cfg()
    with pipeline.session(cfg) as (conn, http):
        todo = db.to_check(conn, listing_ids, favs, limit)
        changed = pipeline.check(cfg, conn, http, todo)
    out = [{"id": l["id"], "old_status": l["old_status"], "status": l["status"], "url": l["url"]} for l in changed]
    text = f"Checked {len(todo)} listings." + ("".join(
        f"\n#{c['id']}: {pipeline.STATUS_WORDS[c['status']]}  {c['url']}" for c in out) or " No changes.")
    if done:
        _telegram("✅ " + text)
    _out({"checked": len(todo), "changed": out}, as_json, lambda d: typer.echo(text))



@app.command()
def details(limit: Annotated[int, typer.Option(help="Listing pages to fetch (copies from the same house on "
                                               "another site are free).")] = 40,
            bg: Bg = False, done: Done = False, as_json: Json = False):
    """Fetch details (sizes, rates, features, listing date) for matches still waiting. Each page once."""
    if bg:
        _spawn(["details", "--limit", str(limit)])
        return _started("details fetch", as_json)
    cfg = _cfg()
    with pipeline.session(cfg) as (conn, http):
        d = pipeline.details(cfg, conn, http, limit)
    text = pipeline.details_line(d).capitalize()
    if done:
        _telegram("✅ " + text)
    _out(d, as_json, lambda _: typer.echo(text))


@app.command()
def rematch(as_json: Json = False):
    """Re-link same houses and re-check stored listings against config.yaml now (no network)."""
    cfg = _cfg()
    conn = db.connect(cfg.paths.db)
    changed = db.relink(conn) + db.rematch(conn, lambda l: matches(l, cfg.search))
    d = {"changed": changed, "matching": conn.execute("SELECT count(*) FROM listings WHERE matches = 1").fetchone()[0],
         "details_waiting": db.details_waiting(conn, cfg.details.max_attempts)}
    _out(d, as_json, lambda d: typer.echo(
        f"{d['changed']} listings changed. {d['matching']} match now; {d['details_waiting']} wait for details "
        "(fetched next run, or `housebot details`)."))


@app.command()
def features(as_json: Json = False):
    """Feature names seen on fetched listings, most common first (for require/exclude_features)."""
    counts: dict[str, int] = {}
    for (f,) in _conn().execute("SELECT features FROM listings WHERE features IS NOT NULL"):
        for name in {feature_key(x) for x in json.loads(f)}:
            counts[name] = counts.get(name, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    _out(dict(rows), as_json, lambda _: typer.echo("\n".join(f"{n:>5}  {k}" for k, n in rows)
                                                   or "No details fetched yet: run `housebot details`."))


if __name__ == "__main__":
    app()
