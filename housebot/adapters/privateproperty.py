"""Private Property (privateproperty.co.za) adapter.

Fetches search result pages per town (or the whole province) and property type, sorted newest
first with price/bed/size filters in the URL, and parses the listing cards
(JSON-LD inside each card plus the card HTML). Parser is tested against
tests/fixtures/privateproperty/.
"""

import json
import logging
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from ..config import SearchConfig
from ..models import Listing, badge_status, property_type, to_float, to_int
from .base import TYPE_SLUGS, BaseAdapter, Page, slug

log = logging.getLogger(__name__)
BASE = "https://www.privateproperty.co.za"
FEATURES = {"Bedrooms": "beds", "Bathrooms": "baths", "Parking spaces": "garages", "Garages": "garages",
            "Land size": "erf_m2", "Erf size": "erf_m2", "Floor size": "floor_m2"}


class PrivateProperty(BaseAdapter):
    name = "privateproperty"
    newest_first = True

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int = 1) -> str:
        # Query names come from the site's own search JS (fp/tp = price, bd/ba = beds/baths,
        # ff/fl = min floor/land size). Let the site pre-filter; match.py re-checks everything.
        filters = [("fp", cfg.price_min), ("tp", cfg.price_max), ("bd", cfg.beds_min), ("ba", cfg.baths_min)]
        if not cfg.unknown_values_pass:  # the site drops listings with no size, so only filter when we would too
            filters += [("ff", cfg.floor_min_m2), ("fl", cfg.erf_min_m2)]
        q = [f"{k}={int(v)}" for k, v in filters if v]
        q += ["sorttype=Date", "sortorder=Descending"] + ([f"page={page}"] if page > 1 else [])
        return f"{BASE}/{TYPE_SLUGS[ptype]}-for-sale/{slug(town) if town else cfg.province}/{loc_id}?" + "&".join(q)

    def parse(self, html, town, province):
        return parse(html, town, province)

    def has_next(self, html, page):
        return f"page={page + 1}" in html

    def listing_status(self, url: str) -> str:
        r = self.http.get(url)
        # A removed listing redirects to a search page (…?archiveId=…) instead of its own URL.
        if r.status_code == 404 or not str(r.url).split("?")[0].endswith(url.rsplit("/", 1)[-1]):
            return "gone"
        return badge_status(HTMLParser(r.text).css_first(".media-container"))

    def town_ids(self, province: str) -> dict[str, int]:
        # Province page lists regions (Boland, Cape Town, ...); each region page lists its towns.
        top = f"/for-sale/{province}"
        regions = parse_links(self.http.get(f"{BASE}{top}/{self.src.province_id}").text, top)
        out = {}
        for region, rid in regions.values():
            towns = parse_links(self.http.get(f"{BASE}{top}/{region}/{rid}").text, f"{top}/{region}")
            out |= {name: tid for name, (_, tid) in towns.items()}
        return out


def parse(html: str, town: str | None = None, province: str | None = None) -> Page:
    page = Page(html=html)
    for card in HTMLParser(html).css("a.listing-result[href], a.featured-listing[href]"):
        try:
            page.listings.append(_card(card, town, province))
        except Exception as e:
            log.warning("privateproperty: skipped card %s: %s", card.attributes.get("href"), e)
            page.errors += 1
    return page


def _card(card: Node, town, province) -> Listing:
    href = card.attributes["href"]
    lid = re.search(r"/(T\d+)$", href).group(1)
    ld = next((d for d in map(_json, card.css("script[type='application/ld+json']"))
               if d.get("@type") == "Residence"), {})
    # addressLocality: "Welgevonden, Stellenbosch"
    locality = [p.strip() for p in ld.get("address", {}).get("addressLocality", "").split(",")]
    title = card.attributes.get("title")
    price = card.css_first("[class*='__price']")
    l = Listing(
        source="privateproperty", source_listing_id=lid, url=urljoin(BASE, href),
        title=title, province=province,
        town=town or (locality[-1] if len(locality) > 1 else None),
        suburb=locality[0] or None,
        property_type=property_type(title),
        price=to_int(price.text()) if price else None,
        agent_name=_text(card, "[class*='__agent-name']"),
        photo_url=(ld.get("photo") or [{}])[0].get("contentUrl"),
        raw={"geo": ld["geo"]} if "geo" in ld else {},
    )
    l.status = badge_status(card)
    if "featured-listing" in card.attributes.get("class", ""):
        l.raw["promoted"] = True  # promoted cards, shown out of date order
    if re.search(r"\bauction\b", card.text().lower()):
        l.listing_kind = "auction"
    for f in card.css("[class*='__feature'][title]"):
        field = FEATURES.get(f.attributes["title"])
        if field:
            val = f.text(strip=True)
            setattr(l, field, to_float(val) if field in ("beds", "baths") else to_int(val))
    return l


def _json(node: Node) -> dict:
    try:
        d = json.loads(node.text())
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _text(node: Node, sel: str) -> str | None:
    n = node.css_first(sel)
    return n.text(strip=True) or None if n else None


def parse_links(html: str, prefix: str) -> dict[str, tuple[str, int]]:
    """Place name -> (slug, location ID) for links shaped <prefix>/<slug>/<id> (regions or towns)."""
    out = {}
    for a in HTMLParser(html).css(f"a[href^='{prefix}/']"):
        m = re.fullmatch(rf"{re.escape(prefix)}/([a-z0-9-]+)/(\d+)", a.attributes["href"])
        if m and a.text(strip=True):
            out[a.text(strip=True)] = (m[1], int(m[2]))
    return out
