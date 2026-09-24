"""The Listing record that every adapter produces, plus small text-parsing helpers
(prices, sizes, property type) shared by the adapters.
"""

import json
import re
from dataclasses import asdict, dataclass, field


@dataclass
class Listing:
    source: str
    source_listing_id: str
    url: str
    title: str | None = None
    province: str | None = None
    town: str | None = None
    suburb: str | None = None
    property_type: str | None = None
    listing_kind: str = "sale"
    status: str = "active"                # active | under_offer | sold | gone
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    garages: int | None = None
    floor_m2: int | None = None
    erf_m2: int | None = None
    agency: str | None = None
    agent_name: str | None = None
    description: str | None = None
    photo_url: str | None = None
    # From the listing's own page (details); None until fetched.
    parking: int | None = None            # open parking, separate from garages
    storeys: int | None = None
    ensuites: int | None = None
    rates: int | None = None              # rates and taxes, R per month
    levies: int | None = None             # R per month
    pets: bool | None = None
    features: list[str] | None = None     # normalized: pool, garden, flatlet, study, ...
    listed_at: str | None = None          # YYYY-MM-DD, the site's listing date
    raw: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        d["features"] = json.dumps(d["features"]) if d["features"] is not None else None
        return d

    @classmethod
    def from_row(cls, row) -> "Listing":
        """Rebuild from a DB row (sqlite3.Row or dict) that has the listing columns."""
        keys = row.keys()
        d = {f: row[f] for f in cls.__dataclass_fields__ if f != "raw" and f in keys}
        if isinstance(d.get("features"), str):  # normalized on read, so old rows pick up new synonyms
            d["features"] = sorted({feature_key(f) for f in json.loads(d["features"])})
        if d.get("pets") is not None:
            d["pets"] = bool(d["pets"])
        return cls(**d)


# Shared parsing helpers for adapters.

SOLD = ".p24_soldBanner, .listing-banner--sold"
UNDER_OFFER = ".p24_underOfferBanner, .listing-banner--offer-pending"


# Different sites, same thing: map to one feature name for config filters.
FEATURE_SYNONYMS = {
    "office": "study", "pet_friendly": "pets", "pets_allowed": "pets", "fibre_internet": "fibre",
    "swimming_pool": "pool", "built_in_braai": "braai", "braai_room": "braai", "en_suite": "ensuite",
    "granny_flat": "flatlet", "cottage": "flatlet", "flatlets": "flatlet",
    "alarm_system": "alarm", "air_conditioner": "aircon", "air_conditioning": "aircon",
    "totally_fenced": "fenced", "partially_fenced": "fenced", "totally_walled": "walled",
    "wheelchair_accessible": "wheelchair", "wheel_chair_friendly": "wheelchair", "wheelchair_friendly": "wheelchair",
    "solar_panels": "solar", "solar_geyser": "solar", "solar_panels_solar_geyser": "solar",
    "backup_battery_inverter": "inverter", "paveway": "paving",
    "fire_place": "fireplace", "open_plan_special_feature": "open_plan",
}


def feature_key(name: str) -> str:
    """ "Pet Friendly" -> "pets", "Built in Braai" -> "braai", "Walk in closet" -> "walk_in_closet"."""
    k = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return FEATURE_SYNONYMS.get(k, k)


def badge_status(node) -> str:
    """Sale status from the site's badges inside one card or one listing's gallery."""
    if node is None:
        return "active"
    if node.css_first(SOLD):
        return "sold"
    return "under_offer" if node.css_first(UNDER_OFFER) else "active"

def to_int(text: str | None) -> int | None:
    """'R 3 950 000' -> 3950000, '957 m²' -> 957, 'POA' -> None."""
    if not text:
        return None
    # Thousands groups only, so "R 4 300 000 3 Bedroom" stops before the 3.
    m = re.search(r"\d+(?:[\s ,]\d{3})*", text)
    return int(re.sub(r"\D", "", m.group())) if m else None


def to_float(text: str | None) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(m.group()) if m else None


# Order matters: "townhouse" and "farm house" must hit before plain "house".
TYPE_WORDS = {
    "townhouse": "townhouse", "town house": "townhouse", "cluster": "townhouse", "duplex": "townhouse", "simplex": "townhouse",
    "apartment": "apartment", "flat": "apartment", "penthouse": "apartment",
    "vacant land": "vacant_land", "plot": "vacant_land", "farm": "farm", "smallholding": "farm",
    "freehold": "house", "freestanding": "house", "estate home": "house",  # agency sites' type names
    "complex home": "townhouse",
    "house": "house",
}


def property_type(title: str | None) -> str | None:
    t = (title or "").lower()
    for word, kind in TYPE_WORDS.items():
        if word in t:
            return kind
    return None
