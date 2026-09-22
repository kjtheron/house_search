"""Private Property (privateproperty.co.za) adapter.

Fetches search result pages per town (or the whole province) and property type, sorted newest
first with price/bed/size filters in the URL, and parses the listing cards
(JSON-LD inside each card plus the card HTML). Parser is tested against
tests/fixtures/privateproperty/.
"""

import json
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from ..config import SearchConfig
from ..models import Listing, badge_status, feature_key, property_type, to_int
from .base import TYPE_SLUGS, BaseAdapter, Page, finish_card, parse_cards, parse_date, slug, text

BASE = "https://www.privateproperty.co.za"
FEATURES = {"Bedrooms": "beds", "Bathrooms": "baths", "Parking spaces": "garages", "Garages": "garages",
            "Land size": "erf_m2", "Erf size": "erf_m2", "Floor size": "floor_m2"}


def parse(html: str) -> Page:
    return parse_cards(html, "a.listing-result[href], a.featured-listing[href]", _card, "privateproperty")


def _card(card: Node) -> Listing | None:
    href = card.attributes["href"]
    m = re.search(r"/(T\d+)$", href)
    if not m:
        return None  # development/project ad, not a single listing
    lid = m[1]
    ld = next((d for d in map(_json, card.css("script[type='application/ld+json']"))
               if d.get("@type") == "Residence"), {})
    # addressLocality: "Welgevonden, Stellenbosch"
    locality = [p.strip() for p in ld.get("address", {}).get("addressLocality", "").split(",")]
    title = card.attributes.get("title")
    # /for-sale/{province}/{region}/{town}/...: province/town from the listing, not the search.
    path = href.strip("/").split("/")
    price = card.css_first("[class*='__price']")
    l = Listing(
        source="privateproperty", source_listing_id=lid, url=urljoin(BASE, href),
        title=title, province=path[1],
        town=locality[-1] if len(locality) > 1 else path[3].replace("-", " ").title(),
        suburb=locality[0] or None,
        property_type=property_type(title),
        price=to_int(price.text()) if price else None,
        agent_name=text(card, "[class*='__agent-name']"),
        photo_url=(ld.get("photo") or [{}])[0].get("contentUrl"),
        raw={"geo": ld["geo"]} if "geo" in ld else {},
    )
    return finish_card(l, card, FEATURES, "[class*='__feature'][title]",
                       promoted="featured-listing" in card.attributes.get("class", ""))


def _json(node: Node) -> dict:
    try:
        d = json.loads(node.text())
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def parse_detail(html: str) -> dict:
    """Listing fields from a listing page's "Property details" and "Property features" lists."""
    t = HTMLParser(html)
    values: dict[str, str] = {}
    features: set[str] = set()
    for li in t.css(".property-details__list-item, .property-features__list-item"):
        v = li.css_first("[class*='__value']")
        val = v.text(strip=True) if v else ""
        name = li.text(separator=" ", strip=True).strip()
        name = name[: -len(val)].strip() if val and name.endswith(val) else name
        if val:
            values.setdefault(name, val)
        else:  # a bare feature: "Pool", "Pet friendly", "Garden"
            features.add(feature_key(name))
    desc = t.css_first(".listing-description__text")
    return {
        "floor_m2": to_int(values.get("Floor size")), "erf_m2": to_int(values.get("Land size")),
        "garages": to_int(values.get("Garage parking")), "parking": to_int(values.get("Open parking")),
        "storeys": to_int(values.get("Storeys")), "ensuites": to_int(values.get("En-suite")),
        "rates": to_int(values.get("Rates and taxes")), "levies": to_int(values.get("Levies")),
        "pets": "pets" in features or None,  # only ever listed when allowed
        "features": sorted(features), "listed_at": parse_date(values.get("Listing date")),
        "description": desc.text(separator=" ", strip=True).strip() if desc else None,
        "status": badge_status(t.css_first(".media-container")),
    }


def parse_links(html: str, prefix: str) -> dict[str, tuple[str, int]]:
    """Place name -> (slug, location ID) for links shaped <prefix>/<slug>/<id> (regions or towns)."""
    out = {}
    for a in HTMLParser(html).css(f"a[href^='{prefix}/']"):
        m = re.fullmatch(rf"{re.escape(prefix)}/([a-z0-9-]+)/(\d+)", a.attributes["href"])
        if m and a.text(strip=True):
            out[a.text(strip=True)] = (m[1], int(m[2]))
    return out


class PrivateProperty(BaseAdapter):
    name = "privateproperty"
    newest_first = True
    next_page = "page={}"
    parse = staticmethod(parse)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int = 1) -> str:
        # Query names come from the site's own search JS (fp/tp = price, bd/ba = beds/baths,
        # ff/fl = min floor/land size). Let the site pre-filter; match.py re-checks everything.
        filters = [("fp", cfg.price_min), ("tp", cfg.price_max), ("bd", cfg.beds_min), ("ba", cfg.baths_min)]
        if not cfg.unknown_values_pass:  # the site drops listings with no size, so only filter when we would too
            filters += [("ff", cfg.floor_min_m2), ("fl", cfg.erf_min_m2)]
        q = [f"{k}={int(v)}" for k, v in filters if v]
        q += ["sorttype=Date", "sortorder=Descending"] + ([f"page={page}"] if page > 1 else [])
        return f"{BASE}/{TYPE_SLUGS[ptype]}-for-sale/{slug(town) if town else cfg.province}/{loc_id}?" + "&".join(q)

    def details(self, url: str) -> dict:
        r = self.http.get(url)
        # A removed listing redirects to a search page (…?archiveId=…) instead of its own URL.
        if r.status_code == 404 or not str(r.url).split("?")[0].endswith(url.rsplit("/", 1)[-1]):
            return {"status": "gone"}
        return parse_detail(r.text)

    def town_ids(self, province: str) -> dict[str, int]:
        # Province page lists regions (Boland, Cape Town, ...); each region page lists its towns.
        top = f"/for-sale/{province}"
        regions = parse_links(self.http.get(f"{BASE}{top}/{self.src.province_id}").text, top)
        out = {}
        for region, rid in regions.values():
            towns = parse_links(self.http.get(f"{BASE}{top}/{region}/{rid}").text, f"{top}/{region}")
            out |= {name: tid for name, (_, tid) in towns.items()}
        return out
