"""The Listing record that every adapter produces, plus small text-parsing helpers
(prices, sizes, property type) shared by the adapters.
"""

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
    raw: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        return d


# Shared parsing helpers for adapters.

SOLD = ".p24_soldBanner, .listing-banner--sold"
UNDER_OFFER = ".p24_underOfferBanner, .listing-banner--offer-pending"


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
    "townhouse": "townhouse", "cluster": "townhouse", "duplex": "townhouse",
    "apartment": "apartment", "flat": "apartment", "penthouse": "apartment",
    "vacant land": "vacant_land", "plot": "vacant_land", "farm": "farm", "smallholding": "farm",
    "house": "house",
}


def property_type(title: str | None) -> str | None:
    t = (title or "").lower()
    for word, kind in TYPE_WORDS.items():
        if word in t:
            return kind
    return None
