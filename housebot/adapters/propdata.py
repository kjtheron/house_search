"""Seeff (seeff.com) and Harcourts (harcourts.co.za) adapters. Both sites run on Propdata, with the
same URLs and page text but different card HTML.

The sites have no province search, so a province search reads the national feed (newest first,
price/bed filters in the URL) and keeps the areas in PROVINCE_AREAS. A town in `locations` searches
just that area. Parser is tested against tests/fixtures/seeff/ and tests/fixtures/harcourts/.
"""

import re
from urllib.parse import urlencode, urljoin

from selectolax.parser import HTMLParser, Node

from ..config import SearchConfig
from ..models import Listing, feature_key, property_type, to_float, to_int
from .base import BaseAdapter, Page, parse_cards, slug, text

# Propdata area slugs (the town part of listing URLs) per province, from the sites' area sitemaps.
# ponytail: Western Cape only; add a province's area slugs here to search it. A new area the
# sites add later is dropped until it is listed here.
PROVINCE_AREAS = {"western-cape": frozenset("""
    agulhas albertinia arniston atlantis aurora baardskeerdersbos barrydale beaufort-west bellville bettys-bay
    blackheath blouberg blue-downs bonnievale bot-river brackenfell bredasdorp calitzdorp cape-town ceres
    clanwilliam darling de-doorns delft durbanville eersterivier fish-hoek franschhoek gansbaai george goodwood
    gordons-bay gouritsmond graafwater grabouw groot-brakrivier hartenbos heidelberg hermanus hopefield hout-bay
    jacobsbaai khayelitsha klawer klein-brak-rivier kleinmond knysna kommetjie kraaifontein kuils-river ladismith
    lamberts-bay langebaan malmesbury mcgregor melkbosstrand milnerton mitchells-plain montagu moorreesburg
    mossel-bay murraysburg napier noordhoek oudtshoorn paarl parow paternoster piketberg plettenberg-bay
    porterville prince-albert pringle-bay redelinghuys riebeek-valley riversdale robertson rooi-els saldanha
    sedgefield simons-town somerset-west st-helena-bay stanford stellenbosch stilbaai strand strandfontein
    struisbaai suiderstrand swellendam touws-river tulbagh vanrhynsdorp velddrif villiersdorp vleesbaai
    vredenburg vredendal wellington wilderness witsand wolseley worcester yzerfontein""".split())}


def province_of(area: str) -> str:
    return next((p for p, areas in PROVINCE_AREAS.items() if area in areas), "other")


def parse(html: str, source: str, base: str, agency: str) -> Page:
    return parse_cards(html, "[data-id][data-model=residential]", lambda c: _card(c, source, base, agency), source)


def _card(card: Node, source: str, base: str, agency: str) -> Listing | None:
    link = card if card.tag == "a" else card.css_first("a[href*='/results/']")
    href = link.attributes.get("href", "") if link else ""
    # /results/residential/for-sale/{area}/{suburb}/{type}/{id}/
    parts = href.split("?")[0].strip("/").split("/")
    if len(parts) < 7 or parts[2] != "for-sale":
        return None  # not a single listing for sale
    heading = text(card, ".card-heading, .card-description") or ""  # "3 Bedroom House For Sale in La Colline"
    stats = " ".join(n.text(strip=True) for n in card.css(".card-stats > *"))
    img = card.css_first("img")
    return Listing(
        source=source, source_listing_id=parts[6], url=urljoin(base, href),
        title=heading or None, province=province_of(parts[3]), town=parts[3].replace("-", " ").title(),
        suburb=heading.split(" in ", 1)[1].strip() if " in " in heading else parts[4].replace("-", " ").title(),
        property_type=property_type(heading) or property_type(parts[5].replace("-", " ")),
        status=_status(card.css(".card-badge")),
        price=to_int(text(card, ".card-price")),
        beds=to_float(_stat(stats, "Bed")), baths=to_float(_stat(stats, "Bath")),
        agency=agency, photo_url=img.attributes.get("src") if img else None,
    )


def _stat(stats: str, word: str) -> str | None:
    m = re.search(rf"([\d.]+)\s*{word}", stats)
    return m[1] if m else None


def _status(badges) -> str:
    words = " ".join(b.text(strip=True).lower() for b in badges)
    return "sold" if "sold" in words else "under_offer" if "offer" in words else "active"


# Headings inside the "Features" block, not features themselves.
SECTIONS = {"interior", "exterior", "sustainability", "additional amenities", "features"}
# Counted rooms that are already columns (or not worth a feature).
COUNTED = re.compile(r"bedroom|bathroom|kitchen|lounge|dining|garage|parking|reception", re.I)


def parse_detail(html: str) -> dict:
    """Listing fields from the listing page text: levy/rates box, "Features", "Sizes", "Listing Info"."""
    t = HTMLParser(html)
    for n in t.css("script, style, svg"):
        n.decompose()
    body = re.sub(r"\|[\s|]*\|", "|", t.body.text(separator="|", strip=True)) if t.body else ""  # drop empty nodes
    get = lambda pat: (m := re.search(pat, body)) and m[1]
    features = set()
    # "Features" block (not the tab bar of the same name), then Harcourts' "Additional Amenities" after "Sizes".
    blocks = re.findall(r"\|(?:Features\|(?=Interior)|Additional Amenities\|)(.*?)\|(?:Sizes|Listing Info|Can't find)", body)
    for item in "|".join(blocks).split("|"):
        item = item.strip("() ")
        name = re.sub(r"^\d+(\.\d+)?\s+(?!hour)", "", item, flags=re.I)  # "1 Study" -> Study, not "24 Hour Access"
        if name and name.lower() not in SECTIONS and not COUNTED.search(name):
            features.add(feature_key(name))
    listed = get(r"Date Listed (\d\d-\d\d-\d\d)")
    badges = [b for b in t.css(".hero-badge, .card-badge") if not _in_card(b)]  # not the similar-listings cards
    desc = t.css_first(".overview-content-intro")
    return {
        "floor_m2": to_int(get(r"Floor Size ([\d\s,.]+)")), "erf_m2": to_int(get(r"Land Size ([\d\s,.]+)")),
        "garages": to_int(get(r"\|(\d+) Garages?\|")), "parking": to_int(get(r"\|(\d+) Parkings?\b")),
        "rates": to_int(get(r"Monthly Rates\|(R[\d\s,]+)")), "levies": to_int(get(r"Monthly Levy\|(R[\d\s,]+)")),
        "pets": "pets" in features or None,
        "features": sorted(features),
        "listed_at": f"20{listed[6:]}-{listed[3:5]}-{listed[:2]}" if listed else None,  # DD-MM-YY
        "description": desc.text(separator=" ", strip=True) if desc else None,
        "status": _status(badges),
    }


def _in_card(node: Node) -> bool:
    while node := node.parent:
        if "data-id" in node.attributes:
            return True
    return False


class Propdata(BaseAdapter):
    base = ""
    agency = ""
    newest_first = True
    next_page = "?page={}"
    per_type = False  # the type is not in the URL on every listing (Harcourts says "freehold"), so read all

    def parse(self, html: str) -> Page:
        return parse(html, self.name, self.base, self.agency)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str | None, page: int = 1) -> str:
        if not town and cfg.province not in PROVINCE_AREAS:
            raise ValueError(f"{self.name}: no area list for province {cfg.province}; set sources.{self.name}.locations")
        # Query names from the site's own search JS. Let the site pre-filter; match.py re-checks everything.
        q = [("page", page)] if page > 1 else []
        q += [(k, int(v)) for k, v in (("min_price", cfg.price_min), ("max_price", cfg.price_max),
                                       ("min_beds", cfg.beds_min), ("min_baths", cfg.baths_min)) if v]
        where = f"{slug(town)}/" if town else ""
        return f"{self.base}/results/residential/for-sale/{where}" + (f"?{urlencode(q)}" if q else "")

    def details(self, url: str) -> dict:
        r = self.http.get(url)
        # A removed listing is a 404 or a redirect to a results page.
        if r.status_code == 404 or url.rstrip("/").rsplit("/", 1)[-1] not in str(r.url):
            return {"status": "gone"}
        return parse_detail(r.text)

    def town_ids(self, province: str) -> dict[str, int]:
        # Searched by area slug, so no ID is needed: 0 is a placeholder.
        return {a.replace("-", " ").title(): 0 for a in PROVINCE_AREAS.get(province, ())}


class Seeff(Propdata):
    name = "seeff"
    base = "https://www.seeff.com"
    agency = "Seeff"


class Harcourts(Propdata):
    name = "harcourts"
    base = "https://www.harcourts.co.za"
    agency = "Harcourts"
