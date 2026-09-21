"""Manage the town filter (search.towns in config.yaml) and look up site town IDs.

Used by `housebot towns list/add/rm` and `housebot backfill`, so the owner can change towns
from Telegram without anyone editing YAML by hand.

- update_config() rewrites only the `towns:` line and the `locations:` line of the sources you
  pass, keeps every comment, and checks the result still loads before saving.
- town_ids() finds each site's location ID for a town (needed for a full-history backfill),
  cached in data/locations.json so the site lists are fetched once.
"""

import difflib
import json
import re
from pathlib import Path

import yaml

from . import config as config_mod
from .adapters import ADAPTERS

TOWNS_LINE = re.compile(r"^(\s+towns:[ \t]*)\[[^\]\n]*\](.*)$", re.M)
LOCATIONS_LINE = re.compile(r"^(\s+locations:[ \t]*)\{[^}\n]*\}(.*)$", re.M)


def _flow(value) -> str:
    return yaml.safe_dump(value, default_flow_style=True, width=10_000, sort_keys=False).strip()


def update_config(towns: list[str] | None = None, locations: dict[str, dict[str, int]] | None = None,
                  path: Path | None = None) -> None:
    """Set search.towns and/or sources.<name>.locations in one checked write (rolled back if invalid)."""
    path = path or config_mod.path()
    text = new = path.read_text()
    if towns is not None:
        if not TOWNS_LINE.search(new):
            raise config_mod.ConfigError(f"{path}: expected a one-line `towns: [...]` under search:")
        new = TOWNS_LINE.sub(lambda m: f"{m[1]}{_flow(towns)}{m[2]}", new, count=1)
    for source, locs in (locations or {}).items():
        head = re.search(rf"^  {re.escape(source)}:[ \t]*$", new, re.M)
        end = re.compile(r"^ {0,2}\S", re.M).search(new, head.end()) if head else None
        block = slice(head.end(), end.start() if end else len(new)) if head else None
        if not block or not LOCATIONS_LINE.search(new[block]):
            raise config_mod.ConfigError(f"{path}: expected a one-line `locations: {{...}}` under {source}:")
        new = new[:block.start] + LOCATIONS_LINE.sub(lambda m: f"{m[1]}{_flow(locs)}{m[2]}", new[block], count=1) \
            + new[block.stop:]
    path.write_text(new)
    try:
        config_mod.load(path)
    except config_mod.ConfigError:
        path.write_text(text)  # put the old file back
        raise


def set_towns(towns: list[str], path: Path | None = None) -> None:
    update_config(towns=towns, path=path)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def resolve(name: str, known: list[str]) -> str | None:
    """Loose match against known town names ("sir lowrys pass" = "Sir Lowry's Pass"); returns the stored spelling."""
    return next((k for k in known if _key(k) == _key(name)), None)


def suggestions(name: str, known: list[str]) -> list[str]:
    return difflib.get_close_matches(name, known, n=5, cutoff=0.6)


# --- site location IDs --------------------------------------------------------

def _cache_file(cfg) -> Path:
    return Path(cfg.paths.db).parent / "locations.json"


def town_ids(cfg, http, town: str, refresh: bool = False) -> dict[str, tuple[str, int] | None]:
    """{source: (site spelling, location ID) or None} for every enabled source."""
    path = _cache_file(cfg)
    try:
        cache = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    out, dirty = {}, False
    for name, src in cfg.sources.items():
        if not src.enabled or name not in ADAPTERS:
            continue
        if refresh or name not in cache:
            cache[name] = ADAPTERS[name](http, src).town_ids(cfg.search.province)
            dirty = True
        hit = resolve(town, list(cache[name]))
        out[name] = (hit, cache[name][hit]) if hit else None
    if dirty:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return out


def all_site_towns(cfg) -> list[str]:
    try:
        cache = json.loads(_cache_file(cfg).read_text())
    except (FileNotFoundError, ValueError):
        return []
    return sorted({t for towns in cache.values() for t in towns})
