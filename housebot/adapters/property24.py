"""Property24 (property24.com) adapter.

Fetches search result pages per town, or for the whole province, per property type, sorted
newest first (price and bed filters go in the URL), and parses the listing tiles.
Parser is tested against tests/fixtures/property24/.
"""

import re
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser, Node

from ..config import SearchConfig
from ..models import Listing, badge_status, property_type, to_int
from .base import TYPE_SLUGS, BaseAdapter, Page, finish_card, parse_cards, slug, text

BASE = "https://www.property24.com"
FEATURES = {"Bedrooms": "beds", "Bathrooms": "baths", "Parking Spaces": "garages", "Garages": "garages",
            "Erf Size": "erf_m2", "Floor Size": "floor_m2"}


def parse(html: str) -> Page:
    return parse_cards(html, ".p24_regularTile[data-listing-number], .p24_proTile[data-listing-number]",
                       _tile, "property24")


def _tile(tile: Node) -> Listing:
    lid = tile.attributes["data-listing-number"]
    href = next(a.attributes["href"] for a in tile.css("a[href]") if f"/{lid.lstrip('P')}" in a.attributes["href"])
    # /for-sale/{suburb}/{town}/{province}/{suburb_id}/{listing_id}
    parts = href.split("?")[0].strip("/").split("/")
    heading = tile.attributes.get("title") or _attr(tile, "div[title]", "title") or ""
    price_node = tile.css_first(".p24_price")
    l = Listing(
        source="property24", source_listing_id=lid, url=urljoin(BASE, href.split("?")[0]),
        title=heading.split(" - ")[0] or None,
        # Town/province from the listing's own URL, never from the search we asked for.
        province=parts[3], town=parts[2].replace("-", " ").title(),
        suburb=text(tile, ".p24_location") or parts[1].replace("-", " ").title(),
        property_type=property_type(heading),
        price=to_int(price_node.attributes.get("content") or price_node.text()) if price_node else None,
        agency=_attr(tile, "[itemtype$=RealEstateAgent] > meta[itemprop=name]", "content"),
        agent_name=text(tile, ".p24_brandingAgentName"),
        description=text(tile, ".p24_excerpt"),
        photo_url=_photo(tile),
    )
    return finish_card(l, tile, FEATURES, ".p24_featureDetails[title]",
                       promoted="p24_proTile" in tile.attributes.get("class", ""))


def _attr(node: Node, sel: str, attr: str) -> str | None:
    n = node.css_first(sel)
    return n.attributes.get(attr) if n else None


def _photo(tile: Node) -> str | None:
    img = tile.css_first("img[itemprop=image]")
    src = img and (img.attributes.get("lazy-src") or img.attributes.get("src"))
    return src if src and src.startswith("http") else None


def parse_towns(html: str, province: str) -> dict[str, int]:
    """Town name -> location ID from the /for-sale/all-cities/<province>/<id> page."""
    out = {}
    for a in HTMLParser(html).css("a[href^='/for-sale/']"):
        m = re.fullmatch(rf"/for-sale/([a-z0-9-]+)/{province}/(\d+)", a.attributes["href"])
        if m and m[1] != "all-cities" and a.text(strip=True):
            out[a.text(strip=True)] = int(m[2])
    return out


class Property24(BaseAdapter):
    name = "property24"
    newest_first = True
    next_page = "/p{}"
    parse = staticmethod(parse)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int = 1) -> str:
        where = f"{slug(town)}/{cfg.province}" if town else cfg.province
        path = f"/{TYPE_SLUGS[ptype]}-for-sale/{where}/{loc_id}" + (f"/p{page}" if page > 1 else "")
        # Let the site pre-filter; match.py re-checks everything.
        sp = [f"{k}={int(v)}" for k, v in (("pf", cfg.price_min), ("pt", cfg.price_max),
                                         ("bd", cfg.beds_min), ("bth", cfg.baths_min)) if v]
        return BASE + path + f"?sp={quote('&'.join(sp + ['so=Newest']))}"

    def listing_status(self, url: str) -> str:
        r = self.http.get(url)
        if r.status_code == 404:
            return "gone"
        return badge_status(HTMLParser(r.text).css_first(".p24_gallery"))

    def town_ids(self, province: str) -> dict[str, int]:
        return parse_towns(self.http.get(f"{BASE}/for-sale/all-cities/{province}/{self.src.province_id}").text,
                           province)
