"""Phase 6b: two more chains — Whole Foods and ShopRite — behind one seam.

Nothing here touches the network. Every parser is driven from a RECORDED
response in tests/fixtures/ (real store, real product, real prices, trimmed to
the fields read), which is the lesson this repo keeps re-learning: a suite that
lives on one side of a seam proves nothing about the other, and a test that
invents its fixture proves nothing about either.

What is pinned:
  - which stores are recognised as a chain we can ask, and that most are not;
  - each chain's parser, against its recorded page or payload;
  - the record contract every adapter meets, so lookup_api never asks which
    chain a record came from;
  - "asked, has no place" and "could not ask" stay apart, all the way up;
  - lookup.py is still the only module that reaches outward — greps for it.
"""

import asyncio
import json
import os
import re
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
import chains
import lookup
import shoprite
import wholefoods

client = TestClient(appmod.app)
SERVER = Path(__file__).parent.parent / "server"
FIX = Path(__file__).parent / "fixtures"

RECORD_KEYS = {
    "sku", "name", "brand", "sub_brand", "pack_size", "upc", "store_number", "amount",
    "unit_price", "aisle", "aisle_side", "section", "shelf", "available", "source",
    "source_url", "fetched_at",
}


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body)


def store_id(name, **extra):
    op(type="store_upsert", store_name=name, **extra)
    return {s["name"]: s for s in client.get("/api/state").json()["stores"]}[name]["id"]


# --- recognising a chain -------------------------------------------------------


def test_most_stores_are_not_a_chain_we_can_ask():
    assert chains.detect("Corner Veg Guy") == ""
    assert chains.detect("McCaffrey's Food Market", "McCaffrey's") == ""


def test_the_three_chains_are_recognised_by_name_or_osm_brand():
    assert chains.detect("Wegmans") == "wegmans"
    assert chains.detect("Whole Foods Market") == "wholefoods"
    assert chains.detect("Some Market", brand="Whole Foods Market") == "wholefoods"
    assert chains.detect("ShopRite of Lawrenceville") == "shoprite"
    assert chains.detect("Shop Rite") == "shoprite"


def test_town_and_state_come_off_an_osm_address():
    addr = "240 Nassau Park Blvd, Princeton, Mercer County, New Jersey, 08540, United States"
    assert chains.address_town_state(addr) == ("princeton", "nj")
    assert chains.postcode(addr) == "08540"
    assert chains.address_town_state("somewhere, Ontario, Canada") == ("", "")


def test_every_state_parses_now_that_a_nationwide_chain_depends_on_it():
    """The list began as the ten states Wegmans trades in. Whole Foods is in
    all of them and forty more; an Austin store must not be "could not read a
    town from the address"."""
    assert chains.address_town_state("525 N Lamar Blvd, Austin, Travis County, Texas, 78703, USA") == ("austin", "tx")
    assert chains.address_town_state("1 Pike Pl, Seattle, King County, Washington, 98101, USA") == ("seattle", "wa")
    assert chains.address_town_state("x, Honolulu, Hawaii, 96813, USA") == ("honolulu", "hi")
    assert len(chains.US_STATES) == 51


# --- the label every chain's aisle is written with ------------------------------


def test_aisle_label_covers_all_three_shapes():
    assert chains.aisle_label({"aisle": "14B", "aisle_side": "L", "section": "11"}) == "Aisle 14B · left · sec 11"
    assert chains.aisle_label({"aisle": "9", "shelf": "7"}) == "Aisle 9 · shelf 7"
    assert chains.aisle_label({"aisle": "Dairy"}) == "Dairy"
    assert chains.aisle_label({"aisle": ""}) == ""


# --- Whole Foods ----------------------------------------------------------------


def test_wholefoods_store_page_names_the_branch():
    st = wholefoods.parse_store((FIX / "wholefoods_store.html").read_text())
    assert st is not None
    assert {k: st[k] for k in ("code", "folder", "name", "state", "postcode")} == {
        "code": "10187", "folder": "princeton", "name": "Princeton", "state": "NJ", "postcode": "08540"}
    assert abs(st["lat"] - 40.3081) < 0.01 and abs(st["lon"] + 74.6682) < 0.01
    assert wholefoods.parse_store("<html>are you a robot?</html>") is None


def test_two_points_are_the_same_shop_only_within_a_car_park():
    assert chains.close(40.3081, -74.6682, 40.3090, -74.6690)          # across the lot
    assert not chains.close(40.3081, -74.6682, 40.3500, -74.6600)      # the next town
    assert not chains.close(None, None, 40.3081, -74.6682)             # unknown confirms nothing


def test_wholefoods_store_cookie_is_the_sites_own_shape():
    import base64
    from urllib.parse import unquote

    raw = base64.b64decode(unquote(wholefoods.store_cookie("10187", "princeton", "Princeton", "NJ")))
    d = json.loads(raw)
    assert d["id"] == "10187" and d["path"] == "princeton" and d["state"] == "NJ"


def test_wholefoods_search_page_yields_priced_records():
    recs = wholefoods.parse_search((FIX / "wholefoods_search.html").read_text(), "10187", "2026-09-07T00:00:00+00:00")
    assert recs is not None and len(recs) == 3
    assert [r["amount"] for r in recs] == [6.29, 6.09, 3.09]
    assert recs[0]["sku"].startswith("B0") and recs[0]["source"] == "wholefoods"
    assert recs[0]["source_url"] == f"https://www.wholefoodsmarket.com/grocery/product/{recs[0]['sku']}"
    assert recs[0]["unit_price"].startswith("$") and "/" in recs[0]["unit_price"]
    for r in recs:
        assert set(r) == RECORD_KEYS
        assert r["aisle"] == ""            # search carries no shelf position
        assert r["fetched_at"] == "2026-09-07T00:00:00+00:00"


def test_wholefoods_empty_search_is_retried_and_never_cached_as_none_stocked(monkeypatch):
    """Amazon answers about half of these searches with a well-formed page that
    lists nothing, then the same query with thirty products a second later. An
    empty page is asked again; one that stays empty is unavailable, not "the
    shop has none" — that would be cached for a fortnight and shown as fact."""
    full = (FIX / "wholefoods_search.html").read_text()
    empty = full.replace('"productsInfo": [', '"productsInfo": [], "x": [', 1)
    answers = iter([(200, empty), (200, full)])
    calls = []

    async def fake(url, *, headers=None, cookies=None, params=None, timeout=25):
        calls.append(url)
        return next(answers, (200, empty))

    monkeypatch.setattr(lookup, "_chrome_get", fake)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    recs = asyncio.run(lookup.products("wholefoods", "milk retry", "10187"))
    assert recs and len(recs) == 3 and len(calls) == 2 + 1     # two searches, then the top hit's page

    calls.clear()
    recs = asyncio.run(lookup.products("wholefoods", "nothing ever", "10187"))
    assert recs is None and len(calls) == 3                     # three tries, then unavailable
    assert lookup.cache_get("product", "wholefoods:10187|nothing ever|8") is None   # and nothing cached


async def _no_sleep(_seconds):
    return None


def test_wholefoods_unreadable_search_is_none_not_empty():
    """A bot wall or an empty shell is a failure to read. Reporting it as [] would
    tell the phone the shop stocks nothing."""
    assert wholefoods.parse_search("<html>Just a moment...</html>", "10187") is None


def test_wholefoods_product_page_places_the_item():
    html = (FIX / "wholefoods_product.html").read_text()
    assert wholefoods.parse_location(html) == "Dairy"
    none_placed = html.replace('"productLocation": "Dairy"', '"productLocation": null')
    assert wholefoods.parse_location(none_placed) == ""       # asked: it has no place
    assert wholefoods.parse_location("<html></html>") is None  # could not ask


# --- ShopRite --------------------------------------------------------------------


def test_shoprite_search_yields_priced_records():
    payload = json.loads((FIX / "shoprite_search.json").read_text())
    recs = shoprite.parse_search(payload, "500", "2026-09-07T00:00:00+00:00")
    assert len(recs) == 3
    assert recs[0]["name"].startswith("Bowl & Basket")
    assert recs[0]["amount"] == 3.49 and recs[0]["unit_price"] == "$6.98/gal"
    assert recs[0]["pack_size"] == "0.5 gal"
    assert recs[0]["sku"] == "00041190467242" and recs[0]["upc"] == recs[0]["sku"]
    # A link a human can open: "Bowl & Basket" must survive as one query, not
    # end at "Bowl" where a bare ampersand would start the next parameter.
    assert "rsid/500" in recs[0]["source_url"]
    assert "q=Bowl+%26+Basket+Chocolate+Milk" in recs[0]["source_url"]
    for r in recs:
        assert set(r) == RECORD_KEYS and r["source"] == "shoprite" and r["aisle"] == ""


def test_shoprite_product_places_the_item():
    payload = json.loads((FIX / "shoprite_product.json").read_text())
    assert shoprite.parse_location(payload) == {"aisle": "Dairy Department"}
    assert shoprite.parse_location({"productLocation": {"aisle": "9", "shelf": "7"}}) == {"aisle": "9", "shelf": "7"}
    assert shoprite.parse_location({"productLocation": None}) == {}       # the corporate placeholder
    assert shoprite.parse_location({"name": "x"}) is None                 # not a product record


def test_shoprite_branch_is_found_by_zip_then_town_and_never_guessed():
    stores = json.loads((FIX / "shoprite_stores.json").read_text())
    by_zip = shoprite.find_branch(stores, "3373 Brunswick Pike, Lawrenceville, Mercer County, New Jersey, 08648, USA")
    assert by_zip == {"rsid": "500", "name": "ShopRite of Lawrenceville"}
    by_town = shoprite.find_branch(stores, "1 Main St, Ewing, Mercer County, New Jersey, USA")
    assert by_town and by_town["rsid"] == "514"
    # Two Montgomerys: the state decides, and the wrong one is never returned.
    nj = shoprite.find_branch(stores, "Rt 206, Montgomery Township, Somerset County, New Jersey, USA")
    assert nj is None or nj["rsid"] != "239"
    # Two branches in one town and no ZIP to tell them apart: neither is an answer.
    assert shoprite.find_branch(stores, "1 Main St, Hamilton Township, Mercer County, New Jersey, USA") is None
    assert shoprite.find_branch(stores, "1 Main St, Princeton, Mercer County, New Jersey, 08540, USA") is None
    assert shoprite.find_branch(stores, "Store address line 1, Edison, New Jersey, 08837, USA") is None  # corporate


def test_shoprite_headers_are_the_four_the_gateway_requires():
    h = shoprite.headers("sess", "corr")
    for k in ("x-site-host", "x-shopping-mode", "x-customer-session-id", "x-correlation-id"):
        assert h[k]
    assert h["x-customer-session-id"].endswith("|sess")


# --- the seam, end to end, with the network stubbed --------------------------------


def _stub_chrome(monkeypatch, routes):
    """`routes`: substring of the URL -> (status, body). Unmatched -> None (a failed
    request), so a test cannot accidentally lean on a call it did not expect."""
    calls = []

    async def fake(url, *, headers=None, cookies=None, params=None, timeout=25):
        calls.append((url, params or {}))
        for frag, res in routes.items():
            if frag in url:
                return res
        return None

    monkeypatch.setattr(lookup, "_chrome_get", fake)
    return calls


def test_linking_a_shoprite_store_resolves_the_branch(monkeypatch):
    _stub_chrome(monkeypatch, {"/api/stores": (200, (FIX / "shoprite_stores.json").read_text())})
    sid = store_id("ShopRite Lawrenceville Test", store_osm_id="way/500",
                   store_address="3373 Brunswick Pike, Lawrenceville, Mercer County, New Jersey, 08648, USA",
                   store_brand="ShopRite")
    d = client.get("/api/stores/link", params={"store_id": sid}).json()
    assert d == {"chain": "shoprite", "chain_store_id": "500"}


def test_linking_a_wholefoods_store_resolves_the_code(monkeypatch):
    _stub_chrome(monkeypatch, {"/stores/princeton": (200, (FIX / "wholefoods_store.html").read_text())})
    sid = store_id("Whole Foods Market Test", store_osm_id="way/10187",
                   store_address="3495 US Highway 1, Princeton, Mercer County, New Jersey, 08540, USA",
                   store_brand="Whole Foods Market")
    d = client.get("/api/stores/link", params={"store_id": sid}).json()
    assert d == {"chain": "wholefoods", "chain_store_id": "10187"}


def test_a_wholefoods_town_page_for_another_branch_is_refused(monkeypatch):
    """/stores/<town> is one store; a town can have several. The page names its
    own ZIP and coordinates, and unless the pin matches one of them the page is
    some other branch — and so would be every price and aisle after it."""
    page = (FIX / "wholefoods_store.html").read_text()
    _stub_chrome(monkeypatch, {"/stores/princeton": (200, page)})
    # Same town, a different ZIP, no coordinates: refused, and it says which branch the page is.
    sid = store_id("Whole Foods Other Princeton", store_osm_id="way/2",
                   store_address="1 Nassau St, Princeton, Mercer County, New Jersey, 08542, USA",
                   store_brand="Whole Foods Market")
    d = client.get("/api/stores/link", params={"store_id": sid}).json()
    assert d["chain_store_id"] == "" and "08540" in d["reason"] and "not this pin" in d["reason"]
    # No ZIP in the address, but the pin sits on the store: accepted by distance.
    sid = store_id("Whole Foods By Pin", store_osm_id="way/3", store_lat=40.3085, store_lon=-74.668,
                   store_address="US Highway 1, Princeton, Mercer County, New Jersey, USA",
                   store_brand="Whole Foods Market")
    d = client.get("/api/stores/link", params={"store_id": sid}).json()
    assert d == {"chain": "wholefoods", "chain_store_id": "10187"}
    # No ZIP and a pin two towns over: refused rather than guessed.
    sid = store_id("Whole Foods Far Pin", store_osm_id="way/4", store_lat=40.35, store_lon=-74.66,
                   store_address="Somewhere, Princeton, Mercer County, New Jersey, USA",
                   store_brand="Whole Foods Market")
    assert client.get("/api/stores/link", params={"store_id": sid}).json()["chain_store_id"] == ""


def test_a_branch_that_cannot_be_named_says_why(monkeypatch):
    _stub_chrome(monkeypatch, {})       # every request fails
    sid = store_id("ShopRite Nowhere", store_osm_id="way/1",
                   store_address="1 Main St, Princeton, New Jersey, 08540, USA")
    d = client.get("/api/stores/link", params={"store_id": sid}).json()
    # Either the list could not be read, or it was (cached by an earlier test)
    # and no branch sits at that address. Both name the chain and neither
    # guesses a branch.
    assert d["chain_store_id"] == "" and "ShopRite" in d["reason"]


def test_prices_at_shoprite_come_placed_and_cached(monkeypatch):
    """Search, then the shelf position for the product that matters — and the
    second ask is answered from the cache with the position already on it."""
    search = (FIX / "shoprite_search.json").read_text()
    product = json.dumps({"sku": "00041190467242", "productLocation": {"aisle": "9", "shelf": "7"}})
    calls = _stub_chrome(monkeypatch, {"/search": (200, search), "/products/00041190467242": (200, product)})
    sid = store_id("ShopRite Priced", store_osm_id="way/2", store_address="x, Lawrenceville, New Jersey, 08648, USA")
    op(type="store_upsert", store_name="ShopRite Priced", store_chain="shoprite", store_chain_id="500")
    _, r = op(type="add", name="chocolate milk")
    cid = r.json()["result"]["catalog_id"] if "catalog_id" in r.json().get("result", {}) else None
    if cid is None:
        cid = [i for i in client.get("/api/state").json()["items"] if i["name"] == "chocolate milk"][0]["catalog_id"]
    d = client.get("/api/prices", params={"catalog_id": cid}).json()
    q = [x for x in d["quotes"] if x["store_id"] == sid]
    assert q and q[0]["amount"] == 3.49 and q[0]["aisle"] == "Aisle 9 · shelf 7"
    assert q[0]["source"] == "shoprite" and q[0]["source_url"].startswith("https://www.shoprite.com/")
    n = len(calls)
    d2 = client.get("/api/prices", params={"catalog_id": cid}).json()
    assert [x["aisle"] for x in d2["quotes"] if x["store_id"] == sid] == ["Aisle 9 · shelf 7"]
    assert len(calls) == n                       # cache before network


def test_one_chains_sku_does_not_demote_the_others(monkeypatch):
    """A Whole Foods ASIN is not a ShopRite UPC. Sent as "the sku" to every
    store, the ShopRite quote could never be exact and would be filed under
    alternatives — even where the household has already picked its jar there.
    The supplied product is used at its own chain; the others keep their pick."""
    search = (FIX / "shoprite_search.json").read_text()
    product = json.dumps({"sku": "00041190467242", "productLocation": {"aisle": "9", "shelf": "7"}})
    _stub_chrome(monkeypatch, {"/search": (200, search), "/products/00041190467242": (200, product)})
    sid = store_id("ShopRite Scoped", store_osm_id="way/3", store_address="x, Lawrenceville, New Jersey, 08648, USA")
    op(type="store_upsert", store_name="ShopRite Scoped", store_chain="shoprite", store_chain_id="500")
    op(type="add", name="scoped milk")
    cid = [i for i in client.get("/api/state").json()["items"] if i["name"] == "scoped milk"][0]["catalog_id"]
    # The household's ShopRite pick: the very jar the recorded search lists first.
    op(type="product_pick", catalog_id=cid, pick_chain="shoprite", pick_sku="00041190467242",
       pick_name="Bowl & Basket Chocolate Milk, half gallon")
    # Now a Whole Foods pick is made on the phone and sent along, ASIN and all.
    d = client.get("/api/prices", params={"catalog_id": cid, "sku": "B000O6EFHO",
                                          "name": "Lactaid 2% Reduced Fat Milk", "chain": "wholefoods"}).json()
    q = [x for x in d["quotes"] if x["store_id"] == sid]
    assert q and q[0]["exact"] is True and q[0]["amount"] == 3.49
    assert not [x for x in d["alternatives"] if x["store_id"] == sid]


def test_placing_a_cached_search_does_not_make_its_prices_young_again(monkeypatch):
    """A search cached five days ago is still good for an aisle and no longer
    good for a price. Asking for the shelf of a newly picked product must not
    rewrite that row with today's date, or a five-day-old price would pass the
    two-day check as fresh on the next comparison."""
    from datetime import UTC, datetime, timedelta

    search = (FIX / "shoprite_search.json").read_text()
    product = json.dumps({"sku": "00041190467242", "productLocation": {"aisle": "9", "shelf": "7"}})
    _stub_chrome(monkeypatch, {"/search": (200, search), "/products/00041190467242": (200, product)})
    key = "shoprite:3002|old milk|8"
    old = shoprite.parse_search(json.loads(search), "3002", "2026-09-01T00:00:00+00:00")
    lookup._conn.execute(
        "INSERT OR REPLACE INTO lookup_cache(kind, key, payload_json, fetched_at) VALUES(?,?,?,?)",
        ("product", key, json.dumps(old), (datetime.now(UTC) - timedelta(days=5)).isoformat()))
    lookup._conn.commit()
    recs = asyncio.run(lookup.products("shoprite", "old milk", "3002", prefer_sku="00041190467242"))
    assert recs and recs[0]["aisle"] == "9"                    # placed, from the five-day-old search
    assert lookup.cache_get("product", key, lookup.TTL["price"]) is None   # ...which is still stale for a price


def test_an_unplaced_product_is_unknown_not_invented(monkeypatch):
    search = (FIX / "shoprite_search.json").read_text()
    calls = _stub_chrome(monkeypatch, {"/search": (200, search),
                                       "/products/00041190467242": (200, json.dumps({"productLocation": None}))})
    recs = asyncio.run(lookup.products("shoprite", "milk unplaced", "3000"))
    assert recs and recs[0]["aisle"] == "" and chains.aisle_label(recs[0]) == ""
    n = len(calls)
    asyncio.run(lookup.products("shoprite", "milk unplaced", "3000"))
    assert len(calls) == n                       # "has no place" was an answer, and is cached


def test_a_failed_placement_is_asked_again_next_time(monkeypatch):
    search = (FIX / "shoprite_search.json").read_text()
    calls = _stub_chrome(monkeypatch, {"/search": (200, search)})     # product read fails
    # A branch no other test places at, so no cached position can leak in.
    recs = asyncio.run(lookup.products("shoprite", "milk failing", "3001"))
    assert recs and recs[0]["aisle"] == ""
    n = len(calls)
    asyncio.run(lookup.products("shoprite", "milk failing", "3001"))
    assert len(calls) == n + 1                   # the search was cached; the placement retried


def test_products_many_reports_what_it_could_not_ask(monkeypatch):
    search = (FIX / "shoprite_search.json").read_text()
    lookup.cache_put("product", "shoprite:500|cached term|5", [{"sku": "1", "aisle": "Dairy Department", "name": "x"}])
    _stub_chrome(monkeypatch, {"q=asked": (200, search)})
    async def fake(url, *, headers=None, cookies=None, params=None, timeout=25):
        return (200, search) if (params or {}).get("q") == "asked term" else None
    monkeypatch.setattr(lookup, "_chrome_get", fake)
    got, complete = asyncio.run(lookup.products_many("shoprite", ["cached term", "asked term", "dead term"], "500"))
    assert set(got) == {"cached term", "asked term"}
    assert complete is False


def test_two_picks_under_one_term_are_both_placed(monkeypatch):
    """Two items on the list can search as the same words and mean different
    jars. Keeping one preferred sku per term placed the first and left the
    other's aisle blank even though it was among the hits."""
    payload = json.loads((FIX / "shoprite_search.json").read_text())
    a, b = [it["sku"] for it in payload["items"][:2]]
    _stub_chrome(monkeypatch, {
        "/search": (200, json.dumps(payload)),
        f"/products/{a}": (200, json.dumps({"productLocation": {"aisle": "3", "shelf": "1"}})),
        f"/products/{b}": (200, json.dumps({"productLocation": {"aisle": "4", "shelf": "2"}})),
    })
    got, complete = asyncio.run(lookup.products_many("shoprite", ["shared words"], "3003",
                                                     {"shared words": [a, b]}))
    assert complete
    by_sku = {r["sku"]: r["aisle"] for r in got["shared words"]}
    assert by_sku[a] == "3" and by_sku[b] == "4"


def test_an_unknown_chain_is_unavailable_not_wegmans():
    assert asyncio.run(lookup.products("costco", "milk", "1")) is None
    assert asyncio.run(lookup.products_many("costco", ["milk"], "1")) == ({}, False)


# --- the invariant that makes the outbound surface auditable ---------------------


def test_only_lookup_py_reaches_outward():
    """One file to read to know everything about the household's SHOPPING that
    this app sends anywhere. A request from anywhere else — httpx, curl_cffi,
    a browser — is a review failure.

    Three modules are exempt and named, with the reason: llm.py talks to vLLM
    and catalog.py to SearXNG, both on the loopback interface, which never
    leaves the box; calendar_sync.py is the travel feature's Google Calendar
    client, which carries dates and nothing about what the household buys.
    Anything new must be added HERE, with its reason, rather than slipping in."""
    exempt = {"lookup.py", "llm.py", "catalog.py", "calendar_sync.py"}
    offenders = []
    for f in SERVER.glob("*.py"):
        if f.name in exempt:
            continue
        src = f.read_text()
        if re.search(r"^\s*(import|from)\s+(httpx|curl_cffi|playwright)\b", src, re.M):
            offenders.append(f.name)
    assert offenders == []
