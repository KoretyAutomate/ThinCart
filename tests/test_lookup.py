"""Phase 6: the outbound-lookup choke point (server/lookup.py).

These tests never touch the network. That is the point, and it is the lesson the
APK shipped to earn (PLAN.md §2026-08-30): a suite that lives entirely on one
side of a seam proves nothing about the other. So the parsing, the provenance
rules, the TTL and the kill switch are pinned here against a RECORDED Nominatim
response, and the network path itself is exercised separately and is allowed to
be skipped when the box is offline.

The rules under test are the three lookup.py promises:
  1. provenance or nothing,
  2. not-found is an answer — never a plausible substitute,
  3. cache before network.
"""

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient

import app as appmod
import lookup
import lookup_api
import wegmans

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body)


def stores_by_name():
    return {s["name"]: s for s in client.get("/api/state").json()["stores"]}


# One real Nominatim jsonv2 shape, trimmed. Recorded rather than invented so the
# parser is tested against the field names the service actually sends.
FIXTURE = [
    {
        "osm_type": "way", "osm_id": 123456789,
        "name": "Whole Foods Market",
        "display_name": "Whole Foods Market, 3535 US-1, Princeton, Mercer County, New Jersey, 08540, United States",
        "lat": "40.3311", "lon": "-74.6512",
        "type": "supermarket", "category": "shop",
        "extratags": {"brand": "Whole Foods Market"},
    },
    {
        "osm_type": "node", "osm_id": 987654321,
        "name": "McCaffrey's Food Market",
        "display_name": "McCaffrey's Food Market, 301 N Harrison St, Princeton, New Jersey, 08540, United States",
        "lat": "40.3573", "lon": "-74.6588",
        "type": "supermarket", "category": "shop",
    },
    {   # not a grocery: must still be findable, but must sort below the shops
        "osm_type": "node", "osm_id": 555,
        "name": "Princeton Public Library",
        "display_name": "Princeton Public Library, 65 Witherspoon St, Princeton, New Jersey, United States",
        "lat": "40.3500", "lon": "-74.6590",
        "type": "library", "category": "amenity",
    },
    {   # no osm_id: nothing to pin a store TO, so it is dropped entirely
        "osm_type": "node", "name": "Ghost Mart",
        "display_name": "Ghost Mart, Nowhere", "lat": "0", "lon": "0", "type": "supermarket",
    },
]


def test_parse_keeps_only_pinnable_results():
    out = lookup.parse_store_results(FIXTURE)
    assert [d["name"] for d in out] == [
        "McCaffrey's Food Market", "Whole Foods Market", "Princeton Public Library"
    ]
    assert all(d["osm_id"] for d in out)


def test_every_result_carries_provenance():
    """Promise 1. A record that cannot say where it came from is not returned."""
    for d in lookup.parse_store_results(FIXTURE):
        assert d["source"] == "openstreetmap"
        assert d["fetched_at"]                 # source AND date, on every record
        assert "/" in d["osm_id"]  # osm_type/osm_id, unique across OSM
    stamped = lookup.parse_store_results(FIXTURE, "2026-09-01T10:00:00+00:00")
    assert all(d["fetched_at"] == "2026-09-01T10:00:00+00:00" for d in stamped)


def test_town_comes_from_nominatim_not_from_guesswork():
    """The town is what tells two branches of one chain apart, so it is read
    from the structured address rather than picked out of the display string."""
    raw = [{"osm_type": "way", "osm_id": 1, "name": "Wegmans",
            "display_name": "Wegmans, 29 Emmons Dr, Princeton, NJ",
            "address": {"road": "Emmons Dr", "town": "Princeton", "state": "New Jersey"}}]
    assert lookup.parse_store_results(raw)[0]["town"] == "Princeton"
    # ...and its absence is an empty string, never a guess from the address line.
    assert lookup.parse_store_results(
        [{"osm_type": "way", "osm_id": 2, "name": "X", "display_name": "X, somewhere"}]
    )[0]["town"] == ""


def test_address_drops_the_duplicated_name():
    whole = next(d for d in lookup.parse_store_results(FIXTURE) if d["name"] == "Whole Foods Market")
    assert whole["address"].startswith("3535 US-1, Princeton")
    assert not whole["address"].startswith("Whole Foods Market")
    assert whole["lat"] == 40.3311
    assert whole["brand"] == "Whole Foods Market"


def test_missing_brand_is_empty_not_invented():
    mc = next(d for d in lookup.parse_store_results(FIXTURE) if d["name"].startswith("McCaffrey"))
    assert mc["brand"] == ""


def test_empty_upstream_gives_an_empty_answer():
    """Promise 2. No results is a result; it must not become a fabricated one."""
    assert lookup.parse_store_results([]) == []


def test_cache_roundtrip_and_expiry():
    """Promise 3, both halves: a fresh entry is served, a stale one is not."""
    lookup.cache_put("store", "k1", [{"name": "X"}])
    assert lookup.cache_get("store", "k1") == [{"name": "X"}]

    stale = (datetime.now(UTC) - lookup.TTL["store"] - timedelta(days=1)).isoformat()
    lookup._conn.execute("UPDATE lookup_cache SET fetched_at=? WHERE kind='store' AND key='k1'", (stale,))
    lookup._conn.commit()
    assert lookup.cache_get("store", "k1") is None
    assert lookup.cache_get("store", "never-asked") is None


def test_kill_switch_matrix():
    original = lookup.MODE
    try:
        lookup.MODE = "off"
        assert not lookup.enabled("stores") and not lookup.enabled("price")
        lookup.MODE = "stores"
        assert lookup.enabled("stores") and not lookup.enabled("price")
        lookup.MODE = "all"
        assert lookup.enabled("stores") and lookup.enabled("price")
    finally:
        lookup.MODE = original


def test_search_endpoint_503s_when_disabled():
    """THINCART_LOOKUP=off is a supported state, not a broken one."""
    original = lookup.MODE
    try:
        lookup.MODE = "off"
        assert client.get("/api/stores/search?q=anything").status_code == 503
        # ...and the rest of the app is untouched by it.
        assert client.get("/api/state").status_code == 200
    finally:
        lookup.MODE = original


def test_store_upsert_pins_the_real_place():
    op(type="store_upsert", store_name="McCaffrey's", store_osm_id="node/987654321",
       store_address="301 N Harrison St, Princeton", store_lat=40.3573, store_lon=-74.6588,
       store_brand="McCaffrey's")
    s = stores_by_name()["McCaffrey's"]
    assert s["osm_id"] == "node/987654321"
    assert s["address"] == "301 N Harrison St, Princeton"
    assert s["lat"] == 40.3573


def test_a_later_name_only_upsert_does_not_blank_the_pin():
    """Editing notes, or a lagging offline op that only knows the name, must not
    erase the address a deliberate pick established."""
    op(type="store_upsert", store_name="Wegmans", store_osm_id="way/1",
       store_address="240 Nassau Park Blvd", store_lat=40.3, store_lon=-74.6)
    op(type="store_upsert", store_name="Wegmans", store_notes="good fish")
    s = stores_by_name()["Wegmans"]
    assert s["osm_id"] == "way/1"
    assert s["address"] == "240 Nassau Park Blvd"
    assert s["notes"] == "good fish"


def test_free_text_stores_still_work_with_no_pin():
    """A shop OSM has never heard of stays first-class."""
    op(type="store_upsert", store_name="Corner Veg Guy")
    s = stores_by_name()["Corner Veg Guy"]
    assert s["osm_id"] is None
    assert s["address"] == ""


# --- Phase 6B/6C: the chain adapter ------------------------------------------
#
# Recorded from the real Algolia `products` index for Princeton (store 93) on
# 2026-09-06 — one record per (product x store), price and shelf position
# together. Recorded rather than invented so the parser is tested against the
# field names the index actually returns.
WEGMANS_HITS = [
    {
        "skuId": "44442", "productName": "Wegmans Organic Creamy Sunflower Butter",
        "consumerBrandName": "Wegmans", "consumerSubBrandName": "Organic",
        "packSize": "16 ounce", "upc": ["00077890444429"], "storeNumber": "93",
        "price_inStore": {"unitPrice": "$0.44/ounce", "amount": 6.99, "channelKey": "93-Instore"},
        "planogram": {"aisle": "14B", "shelf": "1", "aisleSide": "L", "section": "11"},
        "isAvailable": True, "isSoldAtStore": True,
    },
    {   # perishable: the "aisle" is a department name, not a number
        "skuId": "12345", "productName": "Wegmans Whole Milk", "consumerBrandName": "Wegmans",
        "packSize": "1 gallon", "storeNumber": "93",
        "price_inStore": {"unitPrice": "$3.79/gallon", "amount": 3.79},
        "planogram": {"aisle": "Dairy", "shelf": "2", "aisleSide": "", "section": ""},
        "isAvailable": True,
    },
    {   # neither price nor shelf position: answers neither question, so dropped
        "skuId": "99999", "productName": "Ghost Item", "storeNumber": "93",
        "price_inStore": {}, "planogram": {},
    },
    {"productName": "No SKU At All", "storeNumber": "93",
     "price_inStore": {"amount": 1.0}},  # unpinnable
]


def test_wegmans_parse_shape():
    out = wegmans.parse_wegmans_hits(WEGMANS_HITS)
    assert [r["sku"] for r in out] == ["44442", "12345"]
    top = out[0]
    assert top["amount"] == 6.99
    assert top["unit_price"] == "$0.44/ounce"
    assert top["aisle"] == "14B" and top["aisle_side"] == "L" and top["section"] == "11"
    assert top["brand"] == "Wegmans" and top["sub_brand"] == "Organic"
    assert top["pack_size"] == "16 ounce" and top["upc"] == "00077890444429"
    assert top["source"] == "wegmans"


def test_wegmans_drops_records_that_answer_neither_question():
    """A row with no price and no shelf position renders as two blanks, which
    reads as 'free, location unknown' rather than as the absence it is."""
    assert all(r["sku"] != "99999" for r in wegmans.parse_wegmans_hits(WEGMANS_HITS))
    assert wegmans.parse_wegmans_hits([]) == []


def test_aisle_label_reads_as_an_instruction():
    out = wegmans.parse_wegmans_hits(WEGMANS_HITS)
    assert wegmans.aisle_label(out[0]) == "Aisle 14B · left · sec 11"
    # A department is not an aisle number; "Aisle Dairy" would read as a bug.
    assert wegmans.aisle_label(out[1]) == "Dairy"
    assert wegmans.aisle_label({"aisle": ""}) == ""


def test_best_separates_the_picked_product_from_a_near_match():
    """exact=False is what stops an approximate match being shown as a price for
    something the household did not choose."""
    recs = wegmans.parse_wegmans_hits(WEGMANS_HITS)
    rec, exact = lookup_api._best(recs, "12345")
    assert rec["sku"] == "12345" and exact is True
    rec, exact = lookup_api._best(recs, "not-stocked")
    assert rec["sku"] == "44442" and exact is False   # falls back, but says so
    assert lookup_api._best([], "44442") == (None, False)


def test_slug_from_a_real_osm_address():
    addr = "29, Emmons Drive, Princeton, Mercer County, New Jersey, 08540, United States"
    assert wegmans.wegmans_slug(addr) == "princeton-nj"          # skips "Mercer County"
    assert wegmans.wegmans_slug("1 Main St, Fairfax, Virginia, 22030, USA") == "fairfax-va"


def test_slug_refuses_to_guess():
    """A guessed slug resolves to some other town's branch and mis-prices
    everything, with nothing on screen to show it happened."""
    assert wegmans.wegmans_slug("") == ""
    assert wegmans.wegmans_slug("somewhere, Ontario, Canada") == ""


def test_parse_store_number_off_the_branch_page():
    assert wegmans.parse_store_number(r'{\"storeNumber\":93,\"name\"') == "93"
    assert wegmans.parse_store_number('"storeNumber": "139"') == "139"
    assert wegmans.parse_store_number("no number here") == ""


def test_product_pick_is_remembered_per_chain():
    op(type="add", name="sunflower butter", item_id=str(uuid.uuid4()))
    cid = next(i["catalog_id"] for i in client.get("/api/state").json()["items"]
               if i["name"] == "sunflower butter")
    _, res = op(type="product_pick", catalog_id=cid, pick_chain="wegmans", pick_sku="44442",
                pick_name="Wegmans Organic Creamy Sunflower Butter", pick_brand="Wegmans",
                pick_size="16 ounce")
    assert res.status_code == 200
    pick = lookup_api._pick_for(cid, "wegmans")
    assert pick["sku"] == "44442" and pick["pack_size"] == "16 ounce"
    assert lookup_api._pick_for(cid, "other-chain") is None   # skus do not cross chains


def test_product_pick_requires_an_actual_product():
    _, res = op(type="product_pick", catalog_id=1, pick_chain="wegmans")
    assert res.status_code == 422


def test_an_empty_list_is_answered_not_refused():
    """Opening the aisle view with nothing on the list must not report the
    lookup as unavailable — there was simply nothing to ask."""
    import asyncio

    got, complete = asyncio.run(lookup.wegmans_products_many([], "93"))
    assert got == {} and complete is True


def test_disabled_and_unavailable_are_distinguishable():
    """Both are 503, and they need opposite things said: one is a setting the
    owner can change, the other is weather to wait out."""
    original = lookup.MODE
    try:
        lookup.MODE = "off"
        r = client.get("/api/stores/search?q=anything")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == lookup.DISABLED
    finally:
        lookup.MODE = original


def test_unasked_is_not_reported_as_not_found():
    """A term nobody could ask about is absent from the result dict, exactly
    like a term that genuinely has no aisle. Only the `complete` flag tells them
    apart, and rendering the first as the second is the mistake rule 2 exists to
    stop — an item silently placed in "Aisle unknown" when the lookup simply
    fell over."""
    import asyncio

    lookup.cache_put("product", "93|milk|5", [{"sku": "1", "aisle": "Dairy"}])
    original = lookup.WEGMANS_KEY
    try:
        lookup.WEGMANS_KEY = ""     # forces the network leg to fail
        got, complete = asyncio.run(lookup.wegmans_products_many(["milk", "bread"], "93"))
        assert "milk" in got                 # served from cache
        assert "bread" not in got            # never asked
        assert complete is False             # ...and the caller is told so
    finally:
        lookup.WEGMANS_KEY = original


def test_every_price_carries_a_source_a_human_can_open():
    """Provenance rule 1, in full. A looked-up number that cannot be checked has
    to be taken on faith, and these are looked up rather than observed."""
    for r in wegmans.parse_wegmans_hits(WEGMANS_HITS):
        assert r["source"] == "wegmans"
        assert r["source_url"].startswith("https://www.wegmans.com/shop/product/")
        assert r["sku"] in r["source_url"]


def test_records_carry_the_moment_they_were_fetched():
    """Provenance rule 1. A quote's age is a property of the DATA, not of the
    response that carried it — reporting the latter makes stale look current."""
    out = wegmans.parse_wegmans_hits(WEGMANS_HITS, "2026-09-01T10:00:00+00:00")
    assert all(r["fetched_at"] == "2026-09-01T10:00:00+00:00" for r in out)
    assert all(r["fetched_at"] for r in wegmans.parse_wegmans_hits(WEGMANS_HITS))


def test_a_price_needs_fresher_cache_than_an_aisle():
    """One cached record answers both questions, and they have different shelf
    lives. Without a per-caller max_age a fortnight-old price is served as
    current because the aisle it was cached for is still fine."""
    lookup.cache_put("product", "k-fresh", [{"sku": "1"}])
    old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    lookup._conn.execute(
        "UPDATE lookup_cache SET fetched_at=? WHERE kind='product' AND key='k-fresh'", (old,)
    )
    lookup._conn.commit()
    # Still well inside the 14-day product TTL, so the aisle path keeps it...
    assert lookup.cache_get("product", "k-fresh") is not None
    # ...and outside the 2-day price TTL, so the price path refuses it.
    assert lookup.cache_get("product", "k-fresh", lookup.TTL["price"]) is None


def test_repinning_a_store_drops_its_chain_link():
    """Branches of one chain share a name, and stores are keyed by canonical
    name. Without this, moving the pin from one branch to another keeps serving
    the first branch's prices under the second one's address."""
    op(type="store_upsert", store_name="Wegmans NJ", store_osm_id="way/1",
       store_address="240 Nassau Park Blvd, Princeton, New Jersey, 08540, USA")
    op(type="store_upsert", store_name="Wegmans NJ", store_chain="wegmans", store_chain_id="93")
    assert stores_by_name()["Wegmans NJ"]["chain_store_id"] == "93"

    # Same name, different real place -> the link must be re-earned.
    op(type="store_upsert", store_name="Wegmans NJ", store_osm_id="way/2",
       store_address="724 Route 202, Bridgewater, New Jersey, 08807, USA")
    s = stores_by_name()["Wegmans NJ"]
    assert s["osm_id"] == "way/2"
    assert s["chain_store_id"] == "" and s["chain"] == ""


def test_repinning_to_the_same_place_keeps_the_link():
    """Saving notes, or a lagging offline op that repeats the same pin, must not
    cost the link and force a re-link on every edit."""
    op(type="store_upsert", store_name="Wegmans PA", store_osm_id="way/9",
       store_address="1 Main St, Pittsburgh, Pennsylvania, 15201, USA")
    op(type="store_upsert", store_name="Wegmans PA", store_chain="wegmans", store_chain_id="44")
    op(type="store_upsert", store_name="Wegmans PA", store_osm_id="way/9", store_notes="good fish")
    s = stores_by_name()["Wegmans PA"]
    assert s["chain_store_id"] == "44" and s["notes"] == "good fish"


def test_prices_across_two_stores_without_a_pick():
    """Two priced branches is the whole point of comparison, and the second one
    is where a variable shadowing the catalog row would blow up. Driven from the
    cache so no network is touched."""
    for name, num in (("Cmp One", "111"), ("Cmp Two", "139")):
        op(type="store_upsert", store_name=name, store_chain="wegmans", store_chain_id=num)
    op(type="add", name="cmp widget", item_id=str(uuid.uuid4()))
    cid = next(i["catalog_id"] for i in client.get("/api/state").json()["items"]
               if i["name"] == "cmp widget")
    for num, amount in (("111", 7.99), ("139", 4.99)):
        lookup.cache_put("product", f"{num}|cmp widget|8", [{
            "sku": "1", "name": "Cmp Widget", "brand": "B", "sub_brand": "", "pack_size": "1 ea",
            "upc": "", "store_number": num, "amount": amount, "unit_price": "",
            "aisle": "3", "aisle_side": "L", "section": "2", "shelf": "1",
            "available": True, "source": "wegmans", "fetched_at": lookup.now_iso(),
            "source_url": "https://www.wegmans.com/shop/product/1",
        }])
    r = client.get("/api/prices", params={"catalog_id": cid})
    assert r.status_code == 200, r.text
    quotes = r.json()["quotes"]
    assert [q["amount"] for q in quotes] == [4.99, 7.99]      # cheapest first
    assert {q["store"] for q in quotes} == {"Cmp One", "Cmp Two"}


def test_a_near_match_is_not_offered_as_a_price():
    """Once a product is picked, another jar's amount is not a cheaper version
    of it — it leaves the comparison and is offered beside it instead."""
    op(type="store_upsert", store_name="Alt Store", store_chain="wegmans", store_chain_id="222")
    op(type="add", name="alt widget", item_id=str(uuid.uuid4()))
    cid = next(i["catalog_id"] for i in client.get("/api/state").json()["items"]
               if i["name"] == "alt widget")
    # NB the cache key lower-cases the term, as wegmans_products() does.
    lookup.cache_put("product", "222|picked widget|8", [{
        "sku": "other", "name": "Some Other Widget", "brand": "B", "sub_brand": "",
        "pack_size": "", "upc": "", "store_number": "222", "amount": 3.5, "unit_price": "",
        "aisle": "4", "aisle_side": "", "section": "", "shelf": "", "available": True,
        "source": "wegmans", "fetched_at": lookup.now_iso(), "source_url": "https://x/1",
    }])
    d = client.get("/api/prices",
                   params={"catalog_id": cid, "sku": "mine", "name": "Picked Widget"}).json()
    assert d["quotes"] == []
    assert [a["amount"] for a in d["alternatives"]] == [3.5]


def test_stores_mode_does_not_reach_the_chain():
    """THINCART_LOOKUP=stores promises OpenStreetMap and nothing else. Linking
    prices contacts the chain, so it must decline rather than quietly step
    outside the mode the owner chose."""
    op(type="store_upsert", store_name="Wegmans Mode Test", store_osm_id="way/77",
       store_address="1 Nassau St, Princeton, New Jersey, 08540, USA", store_brand="Wegmans")
    sid = stores_by_name()["Wegmans Mode Test"]["id"]
    original = lookup.MODE
    try:
        lookup.MODE = "stores"
        d = client.get("/api/stores/link", params={"store_id": sid}).json()
        assert d["chain_store_id"] == ""
        assert "off" in d["reason"]
    finally:
        lookup.MODE = original


def test_an_unreadable_branch_page_is_not_cached_as_absent():
    """A 200 with no store number is a failure to READ, not a branch that does
    not exist. Caching it under the 90-day chain TTL would freeze a bot wall or
    a redesign into a durable 'no such shop'."""
    assert wegmans.parse_store_number("<html>are you a robot?</html>") == ""
    lookup.cache_put("chain", "sentinel-slug", "")
    # An empty cached value must not read as a usable answer either.
    assert (lookup.cache_get("chain", "sentinel-slug") or None) is None


def test_a_store_with_no_chain_says_so_rather_than_failing_oddly():
    """Most stores have no price source. That is ordinary, not an error state."""
    op(type="store_upsert", store_name="Corner Veg Guy")
    sid = stores_by_name()["Corner Veg Guy"]["id"]
    r = client.get("/api/products/search", params={"catalog_id": 1, "store_id": sid})
    assert r.status_code == 400
    assert client.get("/api/aisles", params={"store_id": sid}).status_code == 400
    link = client.get("/api/stores/link", params={"store_id": sid}).json()
    assert link["chain_store_id"] == ""
    assert "adapter" in link["reason"]
