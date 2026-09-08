"""A pasted link names the shop OpenStreetMap has never heard of.

Pure parsing first — the shapes people actually paste — then the endpoint end
to end with the network stubbed: a chain's store page, a Google pin that
reverse-geocodes to a town the chain spells differently, a short link that
must be followed, and junk, which is refused rather than guessed at.
"""

import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient

import app as appmod
import links
import lookup

client = TestClient(appmod.app)
FIX = Path(__file__).parent / "fixtures"

PLACE = ("https://www.google.com/maps/place/Whole+Foods+Market/@40.4022,-74.6526,17z/"
         "data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d40.402224!4d-74.652613!16s")


# --- the shapes people paste ------------------------------------------------------


def test_a_place_link_yields_the_pin_not_the_viewport():
    p = links.parse_link(PLACE)
    assert p == {"kind": "maps", "name": "Whole Foods Market", "lat": 40.402224, "lon": -74.652613}


def test_the_other_google_shapes():
    assert links.parse_link("https://www.google.com/maps/place/ShopRite/@40.4048,-74.6470,17z")["lat"] == 40.4048
    assert links.parse_link("https://www.google.com/maps?q=40.402224,-74.652613") == {
        "kind": "maps", "name": "", "lat": 40.402224, "lon": -74.652613}
    assert links.parse_link("https://maps.google.com/?q=40.4,-74.6")["lon"] == -74.6
    search = links.parse_link("https://www.google.com/maps/search/whole+foods/@40.40,-74.65,15z")
    assert search["name"] == "whole foods"
    assert links.parse_link("https://www.google.com/maps/place/40.4,-74.6/@40.4,-74.6,17z")["name"] == ""
    # The cookie interstitial wraps the real link.
    from urllib.parse import quote
    wrapped = "https://consent.google.com/m?continue=" + quote(PLACE, safe="")
    assert links.parse_link(wrapped)["lat"] == 40.402224


def test_short_links_are_only_a_redirect():
    assert links.parse_link("https://maps.app.goo.gl/AbCdEf") == {"kind": "short", "url": "https://maps.app.goo.gl/AbCdEf"}
    assert links.parse_link("https://goo.gl/maps/xyz")["kind"] == "short"


def test_a_chains_store_page_is_the_branch():
    assert links.parse_link("https://www.wholefoodsmarket.com/stores/montgomery") == {
        "kind": "chain", "chain": "wholefoods", "slug": "montgomery"}
    assert links.parse_link("https://www.wegmans.com/stores/princeton-nj/") == {
        "kind": "chain", "chain": "wegmans", "slug": "princeton-nj"}
    assert links.parse_link("https://www.shoprite.com/sm/pickup/rsid/617/product/x") == {
        "kind": "chain", "chain": "shoprite", "rsid": "617"}
    assert links.parse_link("https://www.wholefoodsmarket.com/products/milk") is None


def test_junk_and_nameless_maps_links_are_refused():
    for bad in ("", "not a url", "ftp://x", "https://example.com/shop", "https://www.google.com/maps/place/Whole+Foods",
                "https://www.google.com/maps?q=whole+foods", "https://www.google.com/maps/@400,-74.6,17z"):
        assert links.parse_link(bad) is None, bad


def test_town_candidates_try_the_chains_spelling_too():
    assert links.town_candidates("montgomery-township") == ["montgomery-township", "montgomery"]
    assert links.town_candidates("princeton") == ["princeton"]


# --- the endpoint, network stubbed --------------------------------------------------


def _stub(monkeypatch, pages, reverse=None, final=None):
    async def chrome(url, *, headers=None, cookies=None, params=None, timeout=25):
        for frag, res in pages.items():
            if frag in url:
                return res
        return None

    async def nominatim(params, url=""):
        return [reverse] if reverse else None

    async def follow(url):
        return final

    monkeypatch.setattr(lookup, "_chrome_get", chrome)
    monkeypatch.setattr(lookup, "_nominatim", nominatim)
    monkeypatch.setattr(lookup, "follow_link", follow)
    import branches
    monkeypatch.setattr(branches, "follow_link", follow)
    monkeypatch.setattr(branches, "reverse_geocode", lookup.reverse_geocode)


def test_a_whole_foods_store_page_link_is_exact(monkeypatch):
    _stub(monkeypatch, {"/stores/montgomery": (200, (FIX / "wholefoods_store_montgomery.html").read_text())})
    d = client.get("/api/stores/from_link", params={"url": "https://www.wholefoodsmarket.com/stores/montgomery"}).json()
    r = d["result"]
    assert r["chain"] == "wholefoods" and r["chain_store_id"] == "10738"
    assert r["name"] == "Whole Foods Market Skillman" and r["address"] == "Skillman, NJ 08558"
    assert r["osm_id"] == "wholefoods:10738" and abs(r["lat"] - 40.4022) < 0.01
    assert r["brand"] == "Whole Foods"


def test_a_shoprite_page_link_names_its_branch(monkeypatch):
    _stub(monkeypatch, {"/api/stores": (200, (FIX / "shoprite_stores.json").read_text())})
    d = client.get("/api/stores/from_link", params={"url": "https://www.shoprite.com/sm/pickup/rsid/617/"}).json()
    r = d["result"]
    assert r["chain_store_id"] == "617" and r["name"] == "ShopRite of Montgomery, NJ" and "08558" in r["address"]


def test_a_google_pin_becomes_a_pinned_store_with_its_prices(monkeypatch):
    """The Montgomery pin. The map calls the town 'Montgomery Township'; the
    chain's page is /stores/montgomery; the page's own coordinates sit on the
    pin, which is what makes the link trustworthy."""
    reverse = json.loads((FIX / "nominatim_reverse_montgomery.json").read_text())
    _stub(monkeypatch, {"/stores/montgomery-township": (404, ""),
                        "/stores/montgomery": (200, (FIX / "wholefoods_store_montgomery.html").read_text())},
          reverse=reverse)
    d = client.get("/api/stores/from_link", params={"url": PLACE}).json()
    r = d["result"]
    assert r["name"] == "Whole Foods Market"
    assert r["address"].startswith("1200, State Road, Montgomery Township")
    assert (r["lat"], r["lon"]) == (40.402224, -74.652613) and r["osm_id"].startswith("geo:")
    assert r["chain"] == "wholefoods" and r["chain_store_id"] == "10738" and r["link_reason"] == ""


def test_a_pin_far_from_the_towns_page_is_pinned_but_not_linked(monkeypatch):
    """Same town page, a pin two towns over: the store is still added — the pin
    is real — but its prices are not, and the reason says whose page it was."""
    # The same pin's surroundings, but a ZIP that is not the store page's.
    reverse = json.loads((FIX / "nominatim_reverse_montgomery.json").read_text().replace("08558", "08540"))
    _stub(monkeypatch, {"/stores/montgomery": (200, (FIX / "wholefoods_store_montgomery.html").read_text())},
          reverse=reverse)
    far = "https://www.google.com/maps/place/Whole+Foods+Market/@40.35,-74.66,17z/data=!3d40.35!4d-74.66"
    r = client.get("/api/stores/from_link", params={"url": far}).json()["result"]
    assert r["lat"] == 40.35 and r["chain_store_id"] == "" and "not this pin" in r["link_reason"]


def test_a_short_link_is_followed_first(monkeypatch):
    reverse = json.loads((FIX / "nominatim_reverse_montgomery.json").read_text())
    _stub(monkeypatch, {"/stores/montgomery": (200, (FIX / "wholefoods_store_montgomery.html").read_text())},
          reverse=reverse, final=PLACE)
    r = client.get("/api/stores/from_link", params={"url": "https://maps.app.goo.gl/AbCdEf"}).json()["result"]
    assert r and r["chain_store_id"] == "10738"


def test_a_pin_with_no_chain_is_just_a_pinned_store(monkeypatch):
    reverse = json.loads((FIX / "nominatim_reverse_montgomery.json").read_text())
    _stub(monkeypatch, {}, reverse=reverse)
    r = client.get("/api/stores/from_link",
                   params={"url": "https://www.google.com/maps/place/Corner+Veg/@40.4,-74.65,17z"}).json()["result"]
    assert r["name"] == "Corner Veg" and r["chain"] == "" and r["link_reason"] == ""


def test_junk_is_refused_with_a_reason(monkeypatch):
    _stub(monkeypatch, {})
    d = client.get("/api/stores/from_link", params={"url": "https://example.com/somewhere"}).json()
    assert d["result"] is None and "Google Maps" in d["reason"]
