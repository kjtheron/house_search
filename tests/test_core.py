"""Unit tests for matching, fingerprints, the database, the notification rule and the pipeline.

No network: the pipeline tests use a fake adapter. Run with `uv run pytest`.
"""

import pytest

from housebot import db
from housebot.adapters.base import Page
from housebot.config import Config, ConfigError, HttpConfig, SearchConfig, load
from housebot.fingerprint import fingerprint
from housebot.http import PoliteClient
from housebot.match import reasons
from housebot.models import Listing, property_type, to_int
from housebot.notify import format_listing
from housebot.pipeline import collect, notify

SEARCH = SearchConfig(towns=["Stellenbosch"], property_types=["house"], price_min=2_000_000, price_max=4_500_000,
                      beds_min=3, floor_min_m2=150, exclude_keywords=["retirement"])


def house(id="1", source="property24", price=3_000_000, **kw) -> Listing:
    base = dict(town="Stellenbosch", suburb="Die Boord", property_type="house", beds=3, baths=2, erf_m2=600)
    return Listing(source=source, source_listing_id=id, url=f"https://x/{source}/{id}", price=price, **base | kw)


# --- parsing helpers ---------------------------------------------------------

@pytest.mark.parametrize("text,want", [("R 3 950 000", 3950000), ("R 1 450 000", 1450000),
                                       ("POA", None), ("957 m²", 957), ("2091 m²", 2091),
                                       ("R 4 300 000 3 Bedroom House", 4300000), ("12 500 m²", 12500)])
def test_to_int(text, want):
    assert to_int(text) == want


def test_property_type():
    assert [property_type(t) for t in ("3 Bedroom Townhouse", "Farm House", "2 Bedroom House", "Vacant Land")] == \
        ["townhouse", "farm", "house", "vacant_land"]


# --- matching -----------------------------------------------------------------

@pytest.mark.parametrize("kw,ok", [
    ({}, True),
    ({"price": 5_000_000}, False),
    ({"price": 1_000_000}, False),
    ({"price": None}, True),                      # POA, unknown passes
    ({"beds": 2}, False),
    ({"town": "Paarl"}, False),
    ({"property_type": "apartment"}, False),
    ({"floor_m2": 100}, False),
    ({"floor_m2": None}, True),
    ({"description": "Lovely RETIREMENT village"}, False),
    ({"listing_kind": "auction"}, False),
    ({"town": "Cape Town"}, False),
])
def test_match(kw, ok):
    assert (reasons(house(**kw), SEARCH) == []) is ok


def test_exclude_towns_with_province_wide_search():
    wide = SEARCH.model_copy(update={"towns": [], "exclude_towns": ["cape town"]})
    assert reasons(house(town="Paarl"), wide) == []
    assert reasons(house(town="Cape Town"), wide) == ["town Cape Town excluded"]


def test_unknown_values_fail_when_configured():
    strict = SEARCH.model_copy(update={"unknown_values_pass": False})
    assert reasons(house(), strict) == ["floor unknown"]


# --- fingerprint ----------------------------------------------------------------

def test_fingerprint_same_house_across_portals():
    assert fingerprint(house(erf_m2=603)) == fingerprint(house("T9", "privateproperty", erf_m2=598))
    assert fingerprint(house()) != fingerprint(house(beds=4))
    assert fingerprint(house(erf_m2=None)) == "property24:1"  # no size -> per-source key


# --- database -------------------------------------------------------------------

@pytest.fixture
def conn():
    return db.connect(":memory:")


def put(conn, l: Listing, ts=None):
    return db.upsert(conn, l, not reasons(l, SEARCH), fingerprint(l), ts)


def test_upsert_states(conn):
    assert put(conn, house()) == "new"
    assert put(conn, house()) == "unchanged"
    assert put(conn, house(price=2_900_000)) == "price_change"
    conn.execute("UPDATE listings SET status='gone'")
    assert put(conn, house(price=2_900_000)) == "relisted"
    assert [r[0] for r in conn.execute("SELECT price FROM price_history")] == [3_000_000, 2_900_000]


def test_mark_gone_after_three_ok_runs(conn):
    put(conn, house(), ts="2026-01-01T00:00:00")
    for i in range(3):
        rid = db.start_run(conn, "property24")
        db.finish_run(conn, rid, "ok")
        assert db.mark_gone(conn, "property24") == (1 if i == 2 else 0)
    assert conn.execute("SELECT status FROM listings").fetchone()[0] == "gone"


def test_degraded_runs_never_mark_gone(conn):
    put(conn, house(), ts="2026-01-01T00:00:00")
    for _ in range(3):
        db.finish_run(conn, db.start_run(conn, "property24"), "degraded")
    assert db.mark_gone(conn, "property24") == 0


# --- notification rule (plan §6.4, phase 6 checks) --------------------------

def send_all(conn):
    out = db.pending_notifications(conn)
    for l in out:
        db.record_notification(conn, l, None)
    return [(l["id"], l["reason"]) for l in out]


def test_same_listing_twice_notifies_once(conn):
    put(conn, house())
    assert send_all(conn) == [(1, "new")]
    put(conn, house())
    assert send_all(conn) == []


def test_price_change_notifies_again(conn):
    put(conn, house())
    send_all(conn)
    put(conn, house(price=2_800_000))
    pending = db.pending_notifications(conn)
    assert [(p["reason"], p["last_price"], p["price"]) for p in pending] == [("price_change", 3_000_000, 2_800_000)]


def test_same_house_two_portals_notifies_once(conn):
    put(conn, house())
    put(conn, house("T9", "privateproperty"))
    assert send_all(conn) == [(1, "new")]
    put(conn, house("T9", "privateproperty"))
    assert send_all(conn) == []


def test_hidden_never_notifies(conn):
    put(conn, house())
    put(conn, house("T9", "privateproperty"))
    db.hide(conn, 1)
    assert send_all(conn) == []  # hiding covers the same house on the other portal too


def test_non_matching_never_notifies(conn):
    put(conn, house(price=9_000_000))
    assert send_all(conn) == []


# --- pipeline (phase 7 check: second run sends nothing) ---------------------

class FakeAdapter:
    name = "property24"
    complete = True
    listings = [house(), house("2", suburb="Dalsig")]

    def __init__(self, http, src):
        pass

    def pages(self, cfg, known=frozenset()):
        yield Page(listings=list(self.listings))


class Recorder:
    def __init__(self):
        self.msgs = []

    def send(self, text, photo=None):
        self.msgs.append(text)
        return len(self.msgs)


def test_pipeline_second_run_sends_nothing(conn, tmp_path):
    cfg = Config(search=SEARCH, sources={"property24": {"locations": {"Stellenbosch": 459}}},
                 notify={"quiet_if_none": True})
    for expected in (2, 0):
        results = collect(cfg, conn, None, adapters={"property24": FakeAdapter}, debug_dir=tmp_path)
        rec = Recorder()
        assert notify(cfg, conn, rec, results) == expected
    assert rec.msgs == []


def test_pipeline_flags_empty_source(conn, tmp_path):
    class Empty(FakeAdapter):
        listings = []
    cfg = Config(search=SEARCH, sources={"property24": {"locations": {"Stellenbosch": 459}}})
    results = collect(cfg, conn, None, adapters={"property24": Empty}, debug_dir=tmp_path)
    rec = Recorder()
    notify(cfg, conn, rec, results)
    assert results[0]["status"] == "degraded"
    assert any(m.startswith("⚠️ property24 adapter degraded (0 listings)") for m in rec.msgs)


# --- misc ---------------------------------------------------------------------

def test_format_price_drop():
    msg = format_listing({"id": 142, "reason": "price_change", "last_price": 4_200_000, "price": 3_950_000,
                          "town": "Stellenbosch", "suburb": "Die Boord", "property_type": "house", "beds": 3.0,
                          "baths": 2.0, "garages": 2, "erf_m2": 620, "agency": "Christie's <R&D>", "url": "https://x/1"})
    assert "📉 <b>PRICE DROP #142</b>: R 4 200 000 → R 3 950 000 (-6.0%)" in msg
    assert "3 bed · 2 bath · 2 garage" in msg and 'fav 142' in msg
    assert "Agency: Christie's &lt;R&amp;D&gt;" in msg


def test_polite_delay():
    t = [0.0]
    naps = []
    c = PoliteClient(HttpConfig(min_delay_s=5, max_delay_s=10), sleep=lambda s: (naps.append(s), t.__setitem__(0, t[0] + s)),
                     clock=lambda: t[0])
    c._wait("a.com")
    c._wait("b.com")  # another site: no wait
    assert naps == []
    c._wait("a.com")
    assert len(naps) == 1 and 5 <= naps[0] <= 10


def test_bad_config_is_readable(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search: {towns: Paarl, price_min: lots}\nsources: {}\n")
    with pytest.raises(ConfigError) as e:
        load(p)
    assert "search.towns" in str(e.value) and "search.price_min" in str(e.value)


def test_config_needs_location_per_town():
    with pytest.raises(Exception, match="no ID for: Paarl"):
        Config(search=SearchConfig(towns=["Paarl"]), sources={"property24": {"locations": {"Stellenbosch": 459}}})
    with pytest.raises(Exception, match="set locations"):
        Config(search=SearchConfig(), sources={"property24": {}})
    Config(search=SearchConfig(), sources={"property24": {"province_id": 9}})  # province-wide is fine


# --- paging loop ----------------------------------------------------------------

class FakeHttp:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url):
        self.urls.append(url)
        n = int(url.rsplit("=", 1)[1])
        more = f"more{n + 1}" if n < len(self.pages) else ""
        return type("R", (), {"status_code": 200, "text": f"{n}|{self.pages[n - 1]}|{more}"})()


def fake_adapter(newest):
    from housebot.adapters.base import BaseAdapter

    class A(BaseAdapter):
        newest_first = newest
        def search_url(self, cfg, town, loc_id, ptype, page): return f"x?p={page}"
        next_page = "more{}"
        def parse(self, html):
            ls = [house(i) for i in html.split("|")[1].split(",")]
            for l in ls:
                l.raw["promoted"] = l.source_listing_id.startswith("P")
            return Page(listings=ls)
    return A


def test_newest_first_stops_when_caught_up():
    from housebot.config import SourceConfig
    http = FakeHttp(["9,8", "7,6,P1", "5,4", "3,2"])  # unknown promoted P1 doesn't block the stop
    a = fake_adapter(True)(http, SourceConfig(province_id=9))
    got = [l.source_listing_id for p in a.pages(SEARCH, known={"7", "6", "5"}) for l in p.listings]
    assert got == ["9", "8", "7", "6", "P1"] and not a.complete  # page 2 was all known -> stop

    a = fake_adapter(False)(FakeHttp(["9,8", "7,6", "5,4", "3,2"]), SourceConfig(province_id=9))
    assert len([p for p in a.pages(SEARCH, known={"7", "6"})]) == 4 and a.complete


# --- towns, backfill, check -----------------------------------------------------

def test_set_towns_keeps_comments_and_rolls_back(tmp_path):
    from housebot import towns
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  towns: []   # filter\n  exclude_towns: [Paarl]\nsources:\n"
                 "  property24: {province_id: 9}\n")
    towns.set_towns(["Stellenbosch", "Somerset West"], p)
    assert "  towns: [Stellenbosch, Somerset West]   # filter\n" in p.read_text()
    assert load(p).search.towns == ["Stellenbosch", "Somerset West"]
    p.write_text(p.read_text().replace("province_id: 9", "locations: {Stellenbosch: 1}"))
    before = p.read_text()
    with pytest.raises(ConfigError):  # Somerset West has no location ID -> refused, file untouched
        towns.set_towns(["Stellenbosch", "Somerset West"], p)
    assert p.read_text() == before
    assert towns.resolve("somerset west", ["Paarl", "Somerset West"]) == "Somerset West"


def test_delete_town_keeps_favourites(conn):
    put(conn, house())
    put(conn, house("2"))
    put(conn, house("3", town="Paarl"))
    send_all(conn)
    db.fav_add(conn, 2)
    assert db.delete_town(conn, "stellenbosch") == (1, 1)
    assert [r[0] for r in conn.execute("SELECT id FROM listings ORDER BY id")] == [2, 3]


def test_rematch_after_town_change(conn):
    put(conn, house(town="Paarl"))
    assert conn.execute("SELECT matches FROM listings").fetchone()[0] == 0
    wider = SEARCH.model_copy(update={"towns": ["Stellenbosch", "Paarl"]})
    assert db.rematch(conn, lambda l: not reasons(l, wider)) == 1
    assert [p["id"] for p in db.pending_notifications(conn)] == [1]


def test_check_updates_status(conn):
    from housebot.pipeline import check
    put(conn, house(), ts="2026-01-01T00:00:00")
    put(conn, house("2"), ts="2026-01-01T00:00:00")
    put(conn, house("3"))  # seen on a search page just now: no need to open it

    class A:
        def __init__(self, http, src): pass
        def listing_status(self, url): return "sold" if url.endswith("/1") else "active"

    cfg = Config(search=SEARCH, sources={"property24": {"province_id": 9}})
    changed = check(cfg, conn, None, db.to_check(conn), adapters={"property24": A})
    assert [(c["id"], c["status"]) for c in changed] == [(1, "sold")]
    assert [l["id"] for l in db.to_check(conn)] == []  # 1 sold, 2 just checked, 3 just seen
    assert {l["id"] for l in db.search(conn)} == {2, 3}


def test_backfill_never_marks_gone(conn, tmp_path):
    put(conn, house("old"), ts="2026-01-01T00:00:00")
    cfg = Config(search=SEARCH, sources={"property24": {"province_id": 9}})
    for _ in range(3):
        collect(cfg, conn, None, adapters={"property24": FakeAdapter}, debug_dir=tmp_path, backfill=True)
    assert conn.execute("SELECT status FROM listings WHERE source_listing_id='old'").fetchone()[0] == "active"


def test_sources_run_in_parallel_and_fail_alone(conn, tmp_path):
    class Other(FakeAdapter):
        name = "privateproperty"
        listings = [house("T1", "privateproperty", suburb="Dalsig")]

    class Broken(FakeAdapter):
        def pages(self, cfg, known=frozenset()):
            yield Page(listings=[house("9")])
            raise RuntimeError("site changed")

    cfg = Config(search=SEARCH, sources={"property24": {"province_id": 9}, "privateproperty": {"province_id": 4}})
    results = collect(cfg, conn, None, adapters={"property24": Broken, "privateproperty": Other}, debug_dir=tmp_path)
    by = {r["source"]: r for r in results}
    assert by["property24"]["status"] == "failed" and by["property24"]["seen"] == 1
    assert by["privateproperty"]["status"] == "ok" and by["privateproperty"]["seen"] == 1


def test_other_province_never_matches():
    assert reasons(house(province="kwazulu-natal"), SEARCH) == ["province kwazulu-natal"]


def test_wrong_location_id_is_reported():
    from housebot.adapters.base import BaseAdapter
    from housebot.config import SourceConfig

    class Redirecting:
        def get(self, url):
            return type("R", (), {"status_code": 200, "text": "", "url": type("U", (), {
                "path": "/houses-for-sale/umgeni-park/390", "__str__": lambda s: "https://x/houses-for-sale/umgeni-park/390"})()})()

    class A(BaseAdapter):
        name = "privateproperty"
        def search_url(self, *a): return "u"
        def parse(self, html): return Page()

    with pytest.raises(ValueError, match="location ID 390 is not Somerset West"):
        list(A(Redirecting(), SourceConfig(locations={"Somerset West": 390})).pages(SEARCH))


def test_update_config_edits_only_that_sources_locations(tmp_path):
    from housebot import towns
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  towns: [Paarl]  # t\nsources:\n"
                 "  property24:\n    province_id: 9\n    locations: {Paarl: 344}   # p24 ids\n"
                 "  privateproperty:\n    province_id: 4\n    locations: {Paarl: 715}   # pp ids\n")
    towns.update_config(locations={"privateproperty": {"Paarl": 715, "Gordon's Bay": 710}}, path=p)
    text = p.read_text()
    assert "locations: {Paarl: 344}   # p24 ids" in text  # other source untouched
    assert "locations: {Paarl: 715, Gordon's Bay: 710}   # pp ids" in text
    with pytest.raises(ConfigError, match="property24.locations has no ID for: Gordon's Bay"):
        towns.update_config(towns=["Paarl", "Gordon's Bay"], path=p)  # p24 lacks it -> refused
    assert p.read_text() == text  # rolled back


def test_loose_town_names_and_slugs():
    from housebot import towns
    from housebot.adapters.base import slug
    assert towns.resolve("sir lowrys pass", ["Sir Lowry's Pass"]) == "Sir Lowry's Pass"
    assert slug("Gordon's Bay") == "gordons-bay" and slug("Somerset West") == "somerset-west"


def test_towns_add_rm_in_town_mode(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from housebot import cli, towns
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"search:\n  towns: [Paarl]\nsources:\n"
                   f"  property24:\n    province_id: 9\n    locations: {{Paarl: 344}}\n"
                   f"  privateproperty:\n    province_id: 4\n    locations: {{}}\n"
                   f"paths:\n  db: {tmp_path / 'h.db'}\n")
    monkeypatch.setenv("HOUSEBOT_CONFIG", str(cfg))
    monkeypatch.setattr(towns, "town_ids", lambda c, http, t: {"property24": ("Somerset West", 390),
                                                                "privateproperty": ("Somerset West", 711)})
    run = CliRunner().invoke
    r = run(cli.app, ["towns", "add", "somerset west"])
    assert r.exit_code == 0, r.output
    c = load(cfg)
    assert c.search.towns == ["Paarl", "Somerset West"]
    assert c.sources["property24"].locations == {"Paarl": 344, "Somerset West": 390}
    assert c.sources["privateproperty"].locations == {}  # province mode: left alone

    r = run(cli.app, ["towns", "rm", "Paarl"])
    assert r.exit_code == 0, r.output
    assert load(cfg).sources["property24"].locations == {"Somerset West": 390}
    r = run(cli.app, ["towns", "rm", "Somerset West"])
    assert "property24 has no towns left" in r.output and "EMPTY" in r.output
