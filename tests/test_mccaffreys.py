"""McCaffrey's adapter (server/mccaffreys.py), driven from RECORDED answers of
express.mccaffreys.com (tests/fixtures/mccaffreys_*.json, 2026-10-10: the
Princeton branch's search for "egg whites", and the branch list)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import chain_lookup
import chains
import lookup
import mccaffreys

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(mccaffreys, "SITE", "https://express.mccaffreys.com")
SEARCH = json.loads((FIX / "mccaffreys_search.json").read_text())
STORES = mccaffreys.trim_stores(json.loads((FIX / "mccaffreys_stores.json").read_text())) or []

# the household's pin: OpenStreetMap's McCaffrey's in Princeton
PIN = {"name": "McCaffrey's Food Markets", "lat": 40.3645744, "lon": -74.6518416,
       "address": "North Harrison Street, Town & Country, Princeton, Mercer County, New Jersey, 08540, United States"}


def test_the_name_is_recognised_as_the_chain():
    assert chains.detect("McCaffrey's Food Markets") == "mccaffreys"
    assert chains.detect("McCaffreys") == "mccaffreys"
    assert chains.detect("Maruichi Japanese Food & Deli") == ""


def test_search_hits_carry_price_size_and_shelf():
    recs = mccaffreys.parse_search(SEARCH, "1000-7", stamp="2026-10-10T00:00:00+00:00")
    assert recs is not None and len(recs) == 10
    r = recs[1]
    assert (r["name"], r["brand"], r["amount"], r["pack_size"]) == ("Liquid Egg Whites", "Bob Evans", 3.69, "16 OZ")
    assert (r["aisle"], r["aisle_side"]) == ("2", "R")
    assert r["source"] == "mccaffreys" and r["available"] is True
    assert r["source_url"] == "https://express.mccaffreys.com/s/1000-7/i/INV-1000-207418"
    assert chains.aisle_label(r) == "Aisle 2 · right"


def test_multi_buy_fluid_ounces_departments_and_stock():
    payload = {"code": 0, "items": [
        {"id": "a", "name": "Yogurt", "actualPrice": 10.0, "actualPriceDivider": 10, "size": "6 OZ"},
        {"id": "b", "name": "Orange Juice", "actualPrice": 3.49, "actualPriceDivider": 1, "size": "12 FZ",
         "location": "PRODUCE"},
        {"id": "c", "name": "Drumsticks", "actualPrice": 2.99, "size": "1 LB", "location": "MEAT",
         "outOfStock": True, "sellOutOfStock": True},
        {"id": "d", "name": "Gone", "actualPrice": 1.0, "size": "1 EA", "outOfStock": True, "sellOutOfStock": False},
        {"id": "e", "name": "No price", "actualPrice": 0.0},
    ]}
    a, b, c, d = mccaffreys.parse_search(payload, "1000-7") or []
    assert a["amount"] == 1.0                                          # "10 for $10" is $1 each
    assert b["pack_size"] == "12 fl oz" and b["aisle"] == "Produce" and b["aisle_side"] == ""
    assert c["available"] is True and c["amount"] == 2.99               # sold anyway: on the shelf
    assert d["available"] is False


def test_an_unreadable_answer_is_not_an_empty_shop():
    assert mccaffreys.parse_search({"code": 1, "message": "error"}, "1000-7") is None
    assert mccaffreys.parse_search({"code": 0, "items": []}, "1000-7") == []


def test_the_pin_resolves_to_the_princeton_branch():
    b = mccaffreys.find_branch(STORES, PIN)
    assert b and b["id"] == "1000-7" and "Princeton" in b["name"]


def test_coordinates_decide_when_the_address_has_no_zip():
    pin = {**PIN, "address": "North Harrison Street, Princeton"}
    assert (mccaffreys.find_branch(STORES, pin) or {}).get("id") == "1000-7"


def test_never_a_nearest_guess():
    far = {"lat": 40.5, "lon": -74.4, "address": "Somewhere, New Jersey, 08901, United States"}
    assert mccaffreys.find_branch(STORES, far) is None


def test_a_name_only_store_resolves_by_its_town():
    assert (mccaffreys.find_branch(STORES, {"address": ""}, town="pennington") or {}).get("id") == "1000-5097"
    assert mccaffreys.find_branch(STORES, {"address": ""}, town="trenton") is None


def test_a_price_question_reaches_the_adapter(monkeypatch):
    asked = []

    async def fake(term, store, limit):
        asked.append((term, store, limit))
        return mccaffreys.parse_search(SEARCH, store)

    monkeypatch.setattr(lookup, "_mc_search", fake)
    monkeypatch.setattr(lookup, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(lookup, "cache_put", lambda *a, **k: None)
    found, complete = asyncio.run(chain_lookup.price_products_many("mccaffreys", ["egg whites"], "1000-7"))
    assert complete and asked == [("egg whites", "1000-7", chain_lookup.PRICE_LIMIT)]
    assert found["egg whites"][0]["source"] == "mccaffreys"


def test_a_name_only_store_resolves_through_resolve_branch(monkeypatch):
    import branches

    async def stores():
        return STORES

    monkeypatch.setattr(branches, "mccaffreys_stores", stores)
    for name in ("McCaffrey's Pennington", "McCaffreys Pennington", "McCaffrey’s Pennington"):
        assert chains.town_from_name(name, "mccaffreys") == "pennington", name
        got = asyncio.run(branches.resolve_branch("mccaffreys", {"name": name, "address": ""}))
        assert got["chain_store_id"] == "1000-5097" and "Pennington" in got["confirm"], (name, got)
    got = asyncio.run(branches.resolve_branch("mccaffreys", PIN))
    assert got == {"chain_store_id": "1000-7", "reason": ""}


def test_unconfigured_sends_nothing_and_says_so(monkeypatch):
    import branches
    monkeypatch.setattr(mccaffreys, "SITE", "")
    monkeypatch.setattr(lookup, "cache_get", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(lookup, "_mc_post", lambda *a, **k: sent.append(a))
    assert asyncio.run(lookup._mc_search("eggs", "1000-7", 5)) is None and not sent
    got = asyncio.run(branches.resolve_branch("mccaffreys", PIN))
    assert got == {"chain_store_id": "", "reason": mccaffreys.NOT_CONFIGURED}


def test_a_minimum_weight_pack_is_priced_at_its_least_purchase():
    """A per-pound pack with a minimum is a floor, not a package: a wanted pound
    costs the minimum's worth (so a real $3.29 pound wins), and a wanted 4 lb
    costs 4 lb at the per-pound price, not two 3 lb packs."""
    import quantity
    import where

    payload = {"code": 0, "items": [
        {"id": "f", "name": "USDA Chicken Drumsticks Family Pack (3 lb. minimum)", "actualPrice": 2.29,
         "size": "1 LB", "location": "MEAT"},
        {"id": "g", "name": "Chicken Drumsticks min. 2 lbs", "actualPrice": 2.50, "size": "1 LB"},
        {"id": "h", "name": "Mini Peppers", "actualPrice": 3.99, "size": "16 OZ"},
    ]}
    f, g, h = mccaffreys.parse_search(payload, "1000-7") or []
    assert (f["pack_size"], f["amount"], f["min_weight_oz"], f["by_weight"]) == ("1 LB", 2.29, 48, True)
    assert g["min_weight_oz"] == 32
    assert h["min_weight_oz"] == 0 and h["by_weight"] is False        # "Mini" is not a minimum
    pound = {"sku": "p", "name": "Chicken Drumsticks", "pack_size": "1 lb", "amount": 3.29}
    other = {"id": 3, "name": "Elsewhere"}
    store = {"id": 9, "name": "McCaffrey's"}
    one = where.compare([(store, f, False), (other, pound, False)], quantity.parse_wanted("1 lb"))
    assert one["cheapest"]["product"] == "Chicken Drumsticks"
    four = where.compare([(store, f, False), (other, pound, False)], quantity.parse_wanted("4 lb"))
    assert (four["cheapest"]["store"], four["cheapest"]["total"]) == ("McCaffrey's", 9.16)
    part = where.compare([(store, f, False), (other, {**pound, "pack_size": "3.5 lb", "amount": 8.50}, False)],
                         quantity.parse_wanted("3.5 lb"))
    assert (part["cheapest"]["store"], part["cheapest"]["total"]) == ("McCaffrey's", 8.02)   # pro rata
    assert part["cheapest"]["total_label"] == "56 oz = $8.02"
    plain = where.compare([(store, f, False), (other, pound, False)], None)
    assert plain["cheapest"]["store"] == "McCaffrey's"                # unit price is still $2.29/lb
