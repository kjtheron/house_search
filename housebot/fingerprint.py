"""Cross-source dedup key (plan §6.3).

The same house is often listed on several portals. Listings with the same town, suburb,
type, beds, baths and rounded sizes get the same fingerprint, so you get one alert, not two.
"""

import hashlib

from .models import Listing


def fingerprint(l: Listing) -> str:
    """Same house on different portals -> same key. Falls back to a per-source key."""
    sizes = [round(x, -1) if x else None for x in (l.erf_m2, l.floor_m2)]
    if not (l.suburb and l.beds is not None and any(sizes)):
        return f"{l.source}:{l.source_listing_id}"
    parts = [l.town, l.suburb, l.property_type, l.beds, l.baths, *sizes]
    key = "|".join("" if p is None else str(p).lower().strip() for p in parts)
    return hashlib.sha1(key.encode()).hexdigest()
