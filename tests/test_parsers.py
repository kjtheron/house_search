"""Parser tests against saved HTML pages in tests/fixtures/.

If a site changes its HTML, save a fresh page over the fixture, run these, and fix the parser.
After saving a page, run `uv run python tests/redact_fixtures.py` to strip the site's API keys.
"""

from pathlib import Path

from housebot.adapters import privateproperty, property24
from housebot.config import SearchConfig, SourceConfig

FIX = Path(__file__).parent / "fixtures"


def test_property24_search_page():
    page = property24.parse((FIX / "property24/search_houses_stellenbosch.html").read_text(), "Stellenbosch", "western-cape")
    assert page.errors == 0
    assert len(page.listings) == 21
    for l in page.listings:
        assert l.source_listing_id and l.url.startswith("https://www.property24.com/for-sale/")
        assert l.source_listing_id in l.url
        assert l.price and 1_000_000 < l.price < 10_000_000  # URL filtered 2m-4.5m
        assert l.beds and l.baths and l.suburb and l.town == "Stellenbosch"
    first = page.listings[0]
    assert (first.source_listing_id, first.price, first.suburb, first.beds, first.baths, first.garages, first.erf_m2) == \
        ("117508138", 2930000, "Klein Welgevonden", 3, 2, 3, 181)
    assert first.agency == "Coetzenburg Real Estate" and first.property_type == "house"
    assert {l.source_listing_id for l in page.listings if l.listing_kind == "auction"} == {"117549689"}


def test_privateproperty_search_page():
    page = privateproperty.parse((FIX / "privateproperty/search_stellenbosch.html").read_text(), "Stellenbosch", "western-cape")
    assert page.errors == 0
    assert len(page.listings) == 20
    for l in page.listings:
        assert l.source_listing_id.startswith("T") and l.url.endswith(l.source_listing_id)
        assert l.suburb and l.photo_url
    by_id = {l.source_listing_id: l for l in page.listings}
    l = by_id["T5573342"]
    assert (l.price, l.suburb, l.beds, l.baths, l.garages, l.erf_m2, l.property_type) == \
        (7995000, "Welgevonden", 3, 3, 6, 294, "house")
    assert by_id["T5511909"].price is None  # POA
    assert by_id["T5576530"].floor_m2 == 540


def test_search_urls():
    cfg = SearchConfig(towns=["Somerset West"], price_min=2_000_000, price_max=4_500_000, beds_min=3)
    p24 = property24.Property24(None, SourceConfig(locations={"Somerset West": 390}))
    assert p24.search_url(cfg, "Somerset West", 390, "house", 2) == (
        "https://www.property24.com/houses-for-sale/somerset-west/western-cape/390/p2"
        "?sp=pf%3D2000000%26pt%3D4500000%26bd%3D3%26so%3DNewest")
    assert p24.search_url(cfg, None, 9, "house", 1) == (
        "https://www.property24.com/houses-for-sale/western-cape/9"
        "?sp=pf%3D2000000%26pt%3D4500000%26bd%3D3%26so%3DNewest")
    pp = privateproperty.PrivateProperty(None, SourceConfig(locations={"Somerset West": 711}))
    assert pp.search_url(cfg, None, 4, "house", 1) == ("https://www.privateproperty.co.za/houses-for-sale/western-cape/4"
                                                        "?fp=2000000&tp=4500000&bd=3&sorttype=Date&sortorder=Descending")
    assert pp.search_url(cfg, "Somerset West", 711, "townhouse", 3).startswith(
        "https://www.privateproperty.co.za/townhouses-for-sale/somerset-west/711?fp=")
    assert pp.search_url(cfg, "Somerset West", 711, "townhouse", 3).endswith("&page=3")


def test_card_sale_status():
    p24 = property24.parse((FIX / "property24/search_houses_stellenbosch.html").read_text())
    assert {l.source_listing_id: l.status for l in p24.listings}["117508138"] == "under_offer"
    assert {l.status for l in p24.listings} <= {"active", "under_offer", "sold"}


def test_detail_page_status():
    from selectolax.parser import HTMLParser
    from housebot.models import badge_status
    p24 = HTMLParser((FIX / "property24/listing_under_offer.html").read_text())
    pp = HTMLParser((FIX / "privateproperty/listing_under_offer.html").read_text())
    assert badge_status(p24.css_first(".p24_gallery")) == "under_offer"
    assert badge_status(pp.css_first(".media-container")) == "under_offer"
    assert badge_status(HTMLParser('<div class="p24_gallery"><li class="p24_soldBanner">Sold</li></div>')) == "sold"


def test_town_id_lists():
    towns = property24.parse_towns((FIX / "property24/all_cities_western_cape.html").read_text(), "western-cape")
    assert len(towns) > 100 and towns["Paarl"] == 344 and towns["Somerset West"] == 390
    regions = privateproperty.parse_links((FIX / "privateproperty/province_western_cape.html").read_text(),
                                          "/for-sale/western-cape")
    assert regions["Boland"] == ("boland", 50) and regions["Cape Town"] == ("cape-town", 55)
    boland = privateproperty.parse_links((FIX / "privateproperty/region_boland.html").read_text(),
                                         "/for-sale/western-cape/boland")
    assert boland["Stellenbosch"] == ("stellenbosch", 712) and boland["Paarl"] == ("paarl", 715)
