"""Pam Golding (pamgolding.co.za) adapter.

Fetches search result pages per town (or the whole province) and property type, newest first
(the site's default) with the price range in the URL. The pages are Nuxt apps: every listing's data
is in the page's __NUXT_DATA__ JSON, so the parser reads that instead of the HTML.
Parser is tested against tests/fixtures/pamgolding/.
"""

import json
import re
from urllib.parse import urlencode

from selectolax.parser import HTMLParser

from ..config import SearchConfig
from ..models import Listing, feature_key, property_type
from .base import BaseAdapter, Page, slug

BASE = "https://www.pamgolding.co.za"
TYPE_SLUGS = {"house": "houses", "townhouse": "town-houses", "apartment": "apartments",
              "vacant_land": "vacant-land", "farm": "farms"}
WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}


def nuxt_data(html: str) -> list:
    """The page's __NUXT_DATA__ array (devalue format: values refer to other entries by index)."""
    node = HTMLParser(html).css_first("script#__NUXT_DATA__")
    return json.loads(node.text()) if node else []


def _value(d: list, i: int):
    """Rebuild entry i of a devalue array into plain Python values."""
    if i < 0:  # -1 undefined, -2 hole, ...
        return None
    v = d[i]
    if isinstance(v, dict):
        return {k: _value(d, j) for k, j in v.items()}
    if isinstance(v, list):
        if v and isinstance(v[0], str):  # a tagged value: ["Reactive", i], ["Date", "..."], ["Set", i, ...]
            if v[0] in WRAPPERS:
                return _value(d, v[1])
            return v[1] if v[0] == "Date" else [_value(d, j) for j in v[1:]] if v[0] == "Set" else None
        return [_value(d, j) for j in v]
    return v


def _find(d: list, *keys: str):
    """First dict entry that has all of `keys`, rebuilt."""
    return next((_value(d, i) for i, v in enumerate(d) if isinstance(v, dict) and all(k in v for k in keys)), None)


def parse(html: str) -> Page:
    page = Page(html=html)
    res = _find(nuxt_data(html), "pageResults", "totalPages")
    if not res:
        page.errors = 1
        return page
    page.more = res["page"] < res["totalPages"]
    for r in res["pageResults"] or []:
        try:
            page.listings.append(_listing(r))
        except Exception:
            page.errors += 1
    return page


def _listing(r: dict) -> Listing:
    loc, det, price = r["location"], r["details"], r["priceDetail"]
    # "South Africa, Western Cape, Cape Town, Southern Peninsula, Simons Town, Cairnside"
    places = loc["hierarchy"].split(", ")[2:]
    kind = (r.get("type") or {}).get("propertyType") or ""
    images = r.get("searchImages") or []
    return Listing(
        source="pamgolding", source_listing_id=r["webListingReference"], url=BASE + r["seo"]["detailUrlPath"],
        title=r["seo"].get("propertyDescription"), province=slug(loc["provinceName"]),
        town=places[-2] if len(places) >= 3 else places[-1], suburb=loc["lowestLocationDescription"],
        property_type=property_type(kind) or property_type(r["seo"].get("propertyDescription")), status=_status(det),
        price=None if price.get("poa") else price.get("priceZAR"),
        beds=det.get("bedrooms"), baths=det.get("bathrooms"), garages=det.get("garages"),
        floor_m2=det.get("buildingSize"), erf_m2=det.get("erfSize"),
        agency="Pam Golding Properties", description=det.get("marketingTextPreview"),
        photo_url=images[0]["imageUrl"] if images else None, listed_at=(r.get("dateFirstOnWeb") or "")[:10] or None,
    )


def _status(det: dict) -> str:
    return "sold" if det.get("isSold") else "under_offer" if det.get("underOffer") else "active"


def parse_detail(html: str) -> dict:
    """Listing fields from a listing page's JSON: sizes, rates/levies, room and general features."""
    r = _find(nuxt_data(html), "webListingReference", "propertyContent")
    if not r:
        return {"status": "gone"}
    det, content = r["details"], r["propertyContent"] or {}
    features = set()
    for room in content.get("otherRoomFeatures") or []:  # "Study", "Scullery"
        if not re.search(r"kitchen|lounge|dining|family|tv room|entrance|reception", room["description"], re.I):
            features.add(feature_key(room["description"]))
    for f in content.get("generalFeatures") or []:  # {"Garden": "Low maintenance"}, {"Exterior features": "Braai"}
        key, val = f["description"], f.get("detail") or ""
        if key in ("Garden", "Pool", "Security"):
            features.add(feature_key(key))
        features |= {feature_key(x) for x in re.split(r",", val) if key in ("Exterior features", "Security") and x.strip()}
        if "pet friendly" in val.lower():
            features.add("pets")
    rooms = [d["detail"] for g in content.get("roomFeatures") or [] for d in g.get("details") or []]
    costs = {c["amountTypeDescription"]: c["priceDetail"]["priceZAR"] for c in content.get("cashflowExpenses") or []}
    desc = content.get("marketingDescription")
    return {
        "floor_m2": det.get("buildingSize"), "erf_m2": det.get("erfSize"),
        "garages": det.get("garages"), "parking": det.get("parkingBays"),
        "ensuites": sum("en-suite" in d.lower() or "ensuite" in d.lower() for d in rooms) or None,
        "rates": costs.get("Rates and Taxes"), "levies": costs.get("Levies") or costs.get("Levy"),
        "pets": "pets" in features or None,
        "features": sorted(features), "listed_at": (r.get("dateFirstOnWeb") or "")[:10] or None,
        "description": HTMLParser(desc).text(separator=" ", strip=True) if desc else None,
        "status": _status(det),
    }


def parse_towns(xml: str) -> dict[str, int]:
    """Place name -> location ID from the sitemap of search locations ("properties-for-sale-<slug>/<id>")."""
    return {m[1].replace("-", " ").title(): int(m[2])
            for m in re.finditer(r"/property-search/properties-for-sale-([a-z0-9-]+)/(\d+)<", xml)}


class PamGolding(BaseAdapter):
    name = "pamgolding"
    newest_first = True
    parse = staticmethod(parse)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str, page: int = 1) -> str:
        # Query names from the site's search JS (min/max = price). Let the site pre-filter; match.py re-checks.
        q = [(k, int(v)) for k, v in (("min", cfg.price_min), ("max", cfg.price_max)) if v]
        q += [("page", page)] if page > 1 else []
        path = f"/property-search/{TYPE_SLUGS[ptype]}-for-sale-{slug(town) if town else cfg.province}/{loc_id}"
        return BASE + path + (f"?{urlencode(q)}" if q else "")

    def details(self, url: str) -> dict:
        r = self.http.get(url)
        return {"status": "gone"} if r.status_code == 404 else parse_detail(r.text)

    def town_ids(self, province: str) -> dict[str, int]:
        # ponytail: whole-country list; a town name used in two provinces gets the site's plain slug
        return parse_towns(self.http.get(f"{BASE}/sitemaps/propertysearchlocationssale.xml").text)
