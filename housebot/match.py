"""Check a listing against the search criteria in config.yaml.

The sites only pre-filter, so every rule is checked again here. reasons() returns why a
listing fails (handy for debugging); an empty list means it matches.
"""

from .config import SearchConfig
from .models import Listing


def matches(l: Listing, s: SearchConfig) -> bool:
    return not reasons(l, s)


def reasons(l: Listing, s: SearchConfig) -> list[str]:
    """Why the listing fails the search. Empty list = match."""
    out = []

    def at_least(name, value, minimum):
        if minimum is None:
            return
        if value is None:
            if not s.unknown_values_pass:
                out.append(f"{name} unknown")
        elif value < minimum:
            out.append(f"{name} {value} < {minimum}")

    def one_of(name, value, allowed):
        if not allowed:
            return
        if value is None:
            if not s.unknown_values_pass:
                out.append(f"{name} unknown")
        elif value.lower() not in {a.lower() for a in allowed}:
            out.append(f"{name} {value} not wanted")

    one_of("town", l.town, s.towns)
    if l.town and l.town.lower() in {t.lower() for t in s.exclude_towns}:
        out.append(f"town {l.town} excluded")
    one_of("suburb", l.suburb, s.suburbs)
    one_of("type", l.property_type, s.property_types)
    at_least("price", l.price, s.price_min)
    if s.price_max is not None and l.price is not None and l.price > s.price_max:
        out.append(f"price {l.price} > {s.price_max}")
    at_least("beds", l.beds, s.beds_min)
    at_least("baths", l.baths, s.baths_min)
    at_least("garages", l.garages, s.garages_min)
    at_least("floor", l.floor_m2, s.floor_min_m2)
    at_least("erf", l.erf_m2, s.erf_min_m2)
    if l.listing_kind == "auction" and not s.include_auctions:
        out.append("auction")

    text = f"{l.title or ''} {l.description or ''}".lower()
    out += [f"missing '{k}'" for k in s.include_keywords if k.lower() not in text]
    out += [f"has '{k}'" for k in s.exclude_keywords if k.lower() in text]
    return out
