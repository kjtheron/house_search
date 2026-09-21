"""Check a listing against the search criteria in config.yaml.

The sites only pre-filter, so every rule is checked again here. reasons() returns why a
listing fails (handy for debugging); an empty list means it matches.
"""

import re

from .config import SearchConfig
from .models import Listing


def _key(name: str) -> str:
    """Loose place-name key: "Sir Lowry's Pass" == "sir lowrys pass"."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def matches(l: Listing, s: SearchConfig) -> bool:
    return not reasons(l, s)


def reasons(l: Listing, s: SearchConfig) -> list[str]:
    """Why the listing fails the search. Empty list = match."""
    out = []

    num = lambda v: f"{v:g}" if isinstance(v, float) else str(v)

    def between(name, value, lo, hi):
        if lo is None and hi is None:
            return
        if value is None:
            if not s.unknown_values_pass:
                out.append(f"{name} unknown")
        elif lo is not None and value < lo:
            out.append(f"{name} {num(value)} < {num(lo)}")
        elif hi is not None and value > hi:
            out.append(f"{name} {num(value)} > {num(hi)}")

    def one_of(name, value, allowed):
        if not allowed:
            return
        if value is None:
            if not s.unknown_values_pass:
                out.append(f"{name} unknown")
        elif _key(value) not in {_key(a) for a in allowed}:
            out.append(f"{name} {value} not wanted")

    def none_of(name, value, banned):
        if value and _key(value) in {_key(b) for b in banned}:
            out.append(f"{name} {value} excluded")

    if l.province and l.province != s.province:
        out.append(f"province {l.province}")
    one_of("town", l.town, s.towns)
    none_of("town", l.town, s.exclude_towns)
    one_of("suburb", l.suburb, s.suburbs)
    none_of("suburb", l.suburb, s.exclude_suburbs)
    one_of("type", l.property_type, s.property_types)
    between("price", l.price, s.price_min, s.price_max)
    between("beds", l.beds, s.beds_min, s.beds_max)
    between("baths", l.baths, s.baths_min, s.baths_max)
    between("garages", l.garages, s.garages_min, s.garages_max)
    between("floor", l.floor_m2, s.floor_min_m2, s.floor_max_m2)
    between("erf", l.erf_m2, s.erf_min_m2, s.erf_max_m2)
    # ponytail: no site gives garden size; erf - floor is a rough stand-in (a double storey's floor
    # area counts both levels, so it under-estimates). Unknown when either size is missing.
    garden = l.erf_m2 - l.floor_m2 if l.erf_m2 and l.floor_m2 else None
    between("garden", garden, s.garden_min_m2, None)
    if l.listing_kind == "auction" and not s.include_auctions:
        out.append("auction")

    text = f"{l.title or ''} {l.description or ''}".lower()
    out += [f"missing '{k}'" for k in s.include_keywords if k.lower() not in text]
    out += [f"has '{k}'" for k in s.exclude_keywords if k.lower() in text]
    return out
