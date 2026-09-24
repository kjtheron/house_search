"""RE/MAX (remax.co.za) adapter.

The site is a Next.js app: each search page carries its newest 240 listings (all types, newest first) as
JSON in the page's React payload, and ignores filters in the URL (the browser filters and pages through
an API it doesn't publish). So one request per town (or the province) reads everything listed in about
the last two months; match.py filters. More than that is never shown, so a missing listing is not
proof it's gone. Parser is tested against tests/fixtures/remax/.
"""

import json
import re
from datetime import datetime

from selectolax.parser import HTMLParser

from ..config import SearchConfig
from ..models import Listing, feature_key, property_type
from .base import BaseAdapter, Page, slug

BASE = "https://www.remax.co.za"


def next_payload(html: str) -> str:
    """The page's React Server Components payload: the self.__next_f.push([1, "..."]) strings joined."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    return "".join(json.loads(f'"{p}"') for p in parts)


def _object_at(payload: str, marker: str, skip: bool = True):
    """The JSON value right after `marker` (skip=True) or starting at it (skip=False); None if absent."""
    i = payload.find(marker)
    return json.JSONDecoder().raw_decode(payload, i + len(marker) * skip)[0] if i >= 0 else None


def _text_ref(payload: str, ref) -> str | None:
    """Resolve "$3c" to the payload's text chunk `3c:T<hex length>,<text>` (length in UTF-8 bytes)."""
    if not (isinstance(ref, str) and ref.startswith("$")):
        return ref or None
    m = re.search(rf"(?:^|\n|\]){re.escape(ref[1:])}:T([0-9a-f]+),", payload)
    if not m:
        return None
    return payload[m.end():].encode()[: int(m[1], 16)].decode(errors="ignore")


def _status(state: str) -> str:
    s = (state or "").lower()
    return "sold" if "sold" in s else "under_offer" if "offer" in s or "pending" in s else "active"


def _m2(value, units: str) -> int | None:
    if not value:
        return None
    return int(value * 10_000) if (units or "").lower() in ("ha", "hectares") else int(value)


def _listing(d: dict) -> Listing:
    price = d.get("selling_price")
    return Listing(
        source="remax", source_listing_id=str(d["id"]), url=BASE + d["listing_url"],
        title=d.get("marketing_header") or None, province=d.get("province_slug"), town=d.get("city"),
        suburb=d.get("suburb") or None, property_type=property_type(d.get("property_type_name")),
        status=_status(d.get("sale_state")),
        price=price if price and not d.get("price_on_arrival") else None,
        beds=d.get("bedrooms"), baths=d.get("bathrooms"), garages=d.get("garages"),
        floor_m2=_m2(d.get("floor_area"), d.get("floor_area_units")),
        erf_m2=_m2(d.get("land_area"), d.get("land_area_units")),
        rates=d.get("rates") or None, levies=d.get("levy") or None,
        agency=f"RE/MAX {d['office_branch']}" if d.get("office_branch") else "RE/MAX",
        photo_url=d.get("listing_photo_url") or None,
        listed_at=datetime.fromtimestamp(d["published_datetime"]).date().isoformat()
        if d.get("published_datetime") else None,
        raw={"geo": {"latitude": d["latitude"], "longitude": d["longitude"]}} if d.get("latitude") else {},
    )


def parse(html: str) -> Page:
    page = Page(html=html, more=False)  # one page is all the site renders
    data = _object_at(next_payload(html), '"initialJsonLdData":')
    if not data:
        page.errors = 1
        return page
    for d in data.get("listings") or []:
        if d.get("listing_type") != "sale":
            continue
        try:
            page.listings.append(_listing(d))
        except Exception:
            page.errors += 1
    return page


def parse_detail(html: str, listing_id: str) -> dict:
    """Listing fields from a listing page's JSON: the card fields plus `secondary_features`."""
    payload = next_payload(html)
    d = _object_at(payload, f'{{"id":{listing_id},', skip=False) if listing_id.isdigit() else None
    if d is None:
        return {"status": "gone"}
    sec = d.get("secondary_features") or {}
    # {"has_study": true, "are_pets_allowed": true, "num_ensuites": 1, ...}
    features = {feature_key(re.sub(r"^(has|are|is)_", "", k)) for k, v in sec.items()
                if v is True and not k.startswith("smoking")}
    features.discard("kitchen")
    desc = _text_ref(payload, d.get("description"))
    l = _listing(d)
    return {
        "floor_m2": l.floor_m2, "erf_m2": l.erf_m2, "garages": l.garages, "parking": d.get("parkings") or None,
        "ensuites": sec.get("num_ensuites") or None, "rates": l.rates, "levies": l.levies,
        "pets": "pets" in features or None, "features": sorted(features), "listed_at": l.listed_at,
        "description": HTMLParser(desc).text(separator=" ", strip=True) if desc else None,
        "status": l.status,
    }


def parse_towns(xml: str, province: str) -> dict[str, int]:
    """Town name -> 0 (searched by name) from the cities sitemap: /property-for-sale-south-africa/<province>/<town>."""
    return {m[1].replace("-", " ").title(): 0 for m in
            re.finditer(rf"/property-for-sale-south-africa/{re.escape(province)}/([a-z0-9-]+)<", xml)}


class Remax(BaseAdapter):
    name = "remax"
    newest_first = True
    per_type = False  # the page holds every type; match.py filters
    parse = staticmethod(parse)

    def search_url(self, cfg: SearchConfig, town: str | None, loc_id: int, ptype: str | None, page: int = 1) -> str:
        return f"{BASE}/property-for-sale-south-africa/{cfg.province}" + (f"/{slug(town)}" if town else "")

    def pages(self, cfg: SearchConfig, known: set[str] = frozenset()):
        yield from super().pages(cfg, known)
        self.complete = False  # only the newest 240 are shown: an unseen listing may still be for sale

    def details(self, url: str) -> dict:
        r = self.http.get(url)
        lid = url.rsplit("-", 1)[-1]
        # A removed listing redirects to a search page.
        if r.status_code == 404 or not str(r.url).endswith(lid):
            return {"status": "gone"}
        return parse_detail(r.text, lid)

    def town_ids(self, province: str) -> dict[str, int]:
        return parse_towns(self.http.get(f"{BASE}/sitemap-south-africa-cities-for-sale.xml").text, province)
