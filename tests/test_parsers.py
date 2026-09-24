"""Parser tests against saved HTML pages in tests/fixtures/.

If a site changes its HTML, save a fresh page over the fixture, run these, and fix the parser.
After saving a page, run `uv run python tests/redact_fixtures.py` to strip the site's API keys.
"""

from pathlib import Path

from housebot.adapters import pamgolding, privateproperty, propdata, property24
from housebot.config import SearchConfig, SourceConfig

FIX = Path(__file__).parent / "fixtures"


def test_property24_search_page():
    page = property24.parse((FIX / "property24/search_houses_stellenbosch.html").read_text())
    assert page.errors == 0
    assert len(page.listings) == 21
    for l in page.listings:
        assert l.source_listing_id and l.url.startswith("https://www.property24.com/for-sale/")
        assert l.source_listing_id in l.url
        assert l.price and 1_000_000 < l.price < 10_000_000  # URL filtered 2m-4.5m
        assert l.beds and l.baths and l.suburb and l.town == "Stellenbosch" and l.province == "western-cape"
    first = page.listings[0]
    assert (first.source_listing_id, first.price, first.suburb, first.beds, first.baths, first.garages, first.erf_m2) == \
        ("117508138", 2930000, "Klein Welgevonden", 3, 2, 3, 181)
    assert first.agency == "Coetzenburg Real Estate" and first.property_type == "house"
    assert {l.source_listing_id for l in page.listings if l.listing_kind == "auction"} == {"117549689"}


def test_privateproperty_search_page():
    page = privateproperty.parse((FIX / "privateproperty/search_stellenbosch.html").read_text())
    assert page.errors == 0
    assert len(page.listings) == 20
    for l in page.listings:
        assert l.source_listing_id.startswith("T") and l.url.endswith(l.source_listing_id)
        assert l.suburb and l.photo_url and l.town == "Stellenbosch" and l.province == "western-cape"
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


def test_property24_detail_pages():
    d = property24.parse_detail((FIX / "property24/listing_full_features.html").read_text())
    assert (d["floor_m2"], d["erf_m2"], d["garages"], d["parking"], d["rates"]) == (280, 502, 2, 2, 2300)
    assert d["pets"] is True and d["listed_at"] == "2026-09-22" and d["status"] == "active"
    assert {"flatlet", "garden", "braai", "fibre", "alarm", "coastal"} <= set(d["features"])
    assert d["description"].startswith("A Peaceful Coastal Lifestyle")
    assert d["description"].count("A Peaceful Coastal Lifestyle") == 1  # full text only, not the preview too
    d = property24.parse_detail((FIX / "property24/listing_under_offer.html").read_text())
    assert (d["levies"], d["status"], d["floor_m2"], d["erf_m2"]) == (546, "under_offer", 130, 181)
    assert {"pool", "study"} <= set(property24.parse_detail(
        (FIX / "property24/listing_pool_study.html").read_text())["features"])


def test_privateproperty_detail_pages():
    d = privateproperty.parse_detail((FIX / "privateproperty/listing_under_offer.html").read_text())
    assert (d["floor_m2"], d["erf_m2"], d["storeys"], d["garages"], d["parking"], d["rates"]) == (300, 999, 1, 2, 2, 1235)
    assert d["status"] == "under_offer" and d["listed_at"] == "2026-07-29" and d["pets"] is True
    assert d["description"].startswith("Family Living")
    d = privateproperty.parse_detail((FIX / "privateproperty/listing_features.html").read_text())
    assert (d["ensuites"], d["levies"], d["erf_m2"]) == (3, 1000, 923)
    assert {"garden", "fireplace", "scullery", "pets"} <= set(d["features"])


def test_pamgolding_search_page():
    page = pamgolding.parse((FIX / "pamgolding/search_houses_western_cape.html").read_text())
    assert page.errors == 0 and len(page.listings) == 20 and page.more is True
    for l in page.listings:
        assert l.url.startswith("https://www.pamgolding.co.za/property-details/")
        assert l.url.endswith(l.source_listing_id.lower()) and l.province == "western-cape" and l.town and l.suburb
    first = page.listings[0]
    assert (first.source_listing_id, first.price, first.beds, first.baths, first.garages, first.erf_m2) == \
        ("FH1753222", 13_800_000, 6, 4, 2, 476)
    assert (first.town, first.suburb, first.property_type, first.listed_at) == \
        ("Simons Town", "Cairnside", "house", "2026-09-23")
    by_id = {l.source_listing_id: l for l in page.listings}
    assert (by_id["KN1753298"].town, by_id["KN1753298"].suburb) == ("Bettys Bay", "Bettys Bay")  # town-level listing
    assert by_id["VLV1753214"].property_type == "house"  # "Security Estate Home"


def test_pamgolding_detail_page():
    d = pamgolding.parse_detail((FIX / "pamgolding/listing_bettys_bay.html").read_text())
    assert (d["erf_m2"], d["rates"], d["ensuites"], d["status"], d["listed_at"]) == (1021, 1100, 1, "active", "2026-09-23")
    assert {"study", "scullery", "braai", "garden", "alarm", "pets"} <= set(d["features"]) and "kitchen" not in d["features"]
    assert d["pets"] is True and d["description"].startswith("Set against the dramatic mountain")
    assert pamgolding.parse_detail("<html></html>") == {"status": "gone"}


def test_propdata_search_pages():
    seeff = propdata.parse((FIX / "seeff/search_stellenbosch.html").read_text(), "seeff", "https://www.seeff.com", "Seeff")
    harc = propdata.parse((FIX / "harcourts/search_stellenbosch.html").read_text(), "harcourts",
                          "https://www.harcourts.co.za", "Harcourts")
    assert (len(seeff.listings), seeff.errors, len(harc.listings), harc.errors) == (16, 0, 14, 0)
    for l in seeff.listings + harc.listings:
        assert l.source_listing_id in l.url and l.town == "Stellenbosch" and l.province == "western-cape"
        assert l.suburb and l.beds is not None
    l = {l.source_listing_id: l for l in seeff.listings}["3458358"]
    assert (l.price, l.beds, l.baths, l.suburb, l.property_type, l.agency) == \
        (3_250_000, 2, 2, "La Colline", "apartment", "Seeff")
    by_id = {l.source_listing_id: l for l in harc.listings}
    assert by_id["3414865"].status == "under_offer" and by_id["3452054"].property_type == "house"  # "Freehold"
    assert by_id["3452054"].url == "https://www.harcourts.co.za/results/residential/for-sale/stellenbosch/brandwacht/freehold/3452054/"
    assert propdata.province_of("stellenbosch") == "western-cape" and propdata.province_of("durban") == "other"


def test_propdata_detail_pages():
    d = propdata.parse_detail((FIX / "seeff/listing_de_zalze.html").read_text())
    assert (d["floor_m2"], d["erf_m2"], d["garages"], d["rates"], d["levies"], d["status"]) == (413, 380, 4, 3100, 6100, "active")
    assert {"pool", "study", "pets", "solar", "inverter"} <= set(d["features"])
    assert d["description"].startswith("The property is located on the De Zalze")
    d = propdata.parse_detail((FIX / "harcourts/listing_under_offer.html").read_text())
    assert (d["floor_m2"], d["erf_m2"], d["rates"], d["levies"], d["listed_at"]) == (178, 400, 1350, 1300, "2026-06-16")
    assert d["status"] == "under_offer"  # the listing's own gallery badge
    assert {"braai", "fibre", "solar", "pets", "24_hour_access"} <= set(d["features"])


def test_agency_search_urls():
    cfg = SearchConfig(price_min=2_000_000, price_max=2_800_000, beds_min=3)
    pg = pamgolding.PamGolding(None, SourceConfig(province_id=2108))
    assert pg.search_url(cfg, None, 2108, "townhouse", 2) == (
        "https://www.pamgolding.co.za/property-search/town-houses-for-sale-western-cape/2108?min=2000000&max=2800000&page=2")
    assert pg.search_url(cfg, "Somerset West", 2192, "house", 1).startswith(
        "https://www.pamgolding.co.za/property-search/houses-for-sale-somerset-west/2192?")
    s = propdata.Seeff(None, SourceConfig(province_id=0))
    assert s.search_url(cfg, None, 0, None, 3) == (
        "https://www.seeff.com/results/residential/for-sale/?page=3&min_price=2000000&max_price=2800000&min_beds=3")
    assert propdata.Harcourts(None, SourceConfig(province_id=0)).search_url(SearchConfig(), "Somerset West", 0, None, 1) \
        == "https://www.harcourts.co.za/results/residential/for-sale/somerset-west/"
    assert "?page=4" in '<a class="pagination-next" href="?page=4&min_price=1">' and s.next_page.format(4) == "?page=4"


def test_agency_town_lists():
    xml = ("<loc>https://www.pamgolding.co.za/property-search/properties-for-sale-stellenbosch/2932</loc>"
           "<loc>https://www.pamgolding.co.za/property-search/properties-for-sale-somerset-west/2192</loc>")
    assert pamgolding.parse_towns(xml) == {"Stellenbosch": 2932, "Somerset West": 2192}
    assert propdata.Seeff(None, None).town_ids("western-cape")["Somerset West"] == 0
