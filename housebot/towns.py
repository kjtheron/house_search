"""Manage the town filter (search.towns in config.yaml) and look up site town IDs.

Used by `housebot towns list/add/rm` and `housebot backfill`, so the owner can change towns
from Telegram without anyone editing YAML by hand.

- set_towns() rewrites only the `towns:` line, keeps every comment, and checks the result
  still loads before saving.
- town_ids() finds each site's location ID for a town (needed for a full-history backfill),
  cached in data/locations.json so the site lists are fetched once.
"""

import difflib
import json
import os
import re
from pathlib import Path

import yaml

from . import config as config_mod
from .adapters import ADAPTERS

TOWNS_LINE = re.compile(r"^(\s+towns:[ \t]*)\[[^\]\n]*\](.*)$", re.M)


def config_path() -> Path:
    return Path(os.environ.get("HOUSEBOT_CONFIG", "config.yaml"))


def set_towns(towns: list[str], path: Path | None = None) -> None:
    path = path or config_path()
    text = path.read_text()
    if not TOWNS_LINE.search(text):
        raise config_mod.ConfigError(f"{path}: expected a one-line `towns: [...]` under search:")
    flow = yaml.safe_dump(towns, default_flow_style=True, width=10_000).strip()
    new = TOWNS_LINE.sub(lambda m: f"{m[1]}{flow}{m[2]}", text, count=1)
    path.write_text(new)
    try:
        config_mod.load(path)
    except config_mod.ConfigError:
        path.write_text(text)  # put the old file back
        raise


def resolve(name: str, known: list[str]) -> str | None:
    """Case-insensitive match against known town names; returns the stored spelling."""
    return next((k for k in known if k.lower() == name.strip().lower()), None)


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
