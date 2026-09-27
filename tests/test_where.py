"""Phase 7C: where to buy, by price (server/where.py + POST /api/where).

The rules that make "cheapest" an honest claim are pinned here: unit prices
only, one dimension only, organic/brand read from the product's own text, a
pick checked rather than trusted, and "no match" claimed only when every store
answered. The endpoint tests stub `products_many`; nothing touches the network.
"""

import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import pytest
from fastapi.testclient import TestClient

import app as appmod
import lookup
import lookup_api
import where

client = TestClient(appmod.app)


def rec(name, amount, unit_price, **over):
    """A chain product record; `over` sets brand, sub_brand, sku or available."""
    return {"sku": name, "name": name, "brand": "", "sub_brand": "", "pack_size": "",
            "amount": amount, "unit_price": unit_price, "available": True, "source": "wegmans",
            "source_url": "https://x", "fetched_at": "2026-09-27T00:00:00+00:00", **over}


# --- pure rules ---------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("$0.19/ounce", ("weight", 0.19)), ("$0.19/oz", ("weight", 0.19)),
    ("$2.40/lb", ("weight", 0.15)), ("$2.40/lb.", ("weight", 0.15)),
    ("$0.05/fl. oz.", ("volume", 0.05)), ("$0.05/fluid ounce", ("volume", 0.05)),
    ("$3.84/gallon", ("volume", 0.03)), ("$0.50 each", ("count", 0.5)),
    ("$0.30/count", ("count", 0.3)), ("$0.02/sq. ft.", None), ("", None),
])
def test_unit_value(text, want):
    got = where.unit_value(text)
    assert (got is None and want is None) or (got[0] == want[0] and round(got[1], 4) == want[1])


def test_organic_is_read_from_the_products_own_text():
    assert where.is_organic(rec("Wegmans Organic Squash", 1, ""))
    assert where.is_organic(rec("Squash", 1, "", sub_brand="Organic"))
    assert where.is_organic(rec("Avocados", 1, "", brand="Wholesome Pantry Organic"))
    assert not where.is_organic(rec("Wegmans Butternut Squash", 1, ""))


def test_brand_matches_whole_words_and_folds_width():
    assert where.brand_ok(rec("x", 1, "", brand="Horizon"), "horizon")
    assert where.brand_ok(rec("Ｈｏｒｉｚｏｎ Milk", 1, ""), "Horizon")          # full-width folded
    assert not where.brand_ok(rec("Annie's Mac & Cheese", 1, "", brand="Annie's"), "Ann")
    assert where.brand_ok(rec("anything", 1, ""), "")                          # no preference


def test_choose_prefers_the_first_fitting_in_stock_result():
    recs = [rec("Plain Tofu", 1.5, "$0.10/oz"), rec("Organic Tofu", 2, "$0.14/oz", available=False),
            rec("Wegmans Organic Tofu", 2.5, "$0.16/oz")]
    r, status, exact = where.choose(recs, None, True, "")
    assert (r["name"], status, exact) == ("Wegmans Organic Tofu", "ok", False)
    assert where.choose(recs, None, True, "Nasoya")[1] == "no_match"


def test_a_pick_is_checked_not_trusted():
    recs = [rec("Plain Tofu", 1.5, "$0.10/oz", sku="P"), rec("Organic Tofu", 2, "$0.14/oz", sku="O")]
    assert where.choose(recs, "O", True, "")[1:] == ("ok", True)
    assert where.choose(recs, "P", True, "")[1] == "conflict"       # no longer fits: organic now on
    assert where.choose(recs, "Z", False, "")[1] == "pick_missing"  # never swapped for another product


def test_rank_compares_unit_prices_in_one_dimension_only():
    a, b = {"unit_price": "$0.20/oz", "amount": 3.0}, {"unit_price": "$2.40/lb", "amount": 5.0}
    assert where.rank([a, b]) == (b, True)        # $0.15/oz beats $0.20/oz though it costs more
    c = {"unit_price": "$0.50 each", "amount": 1.0}
    assert where.rank([a, c]) == (None, False)    # oz vs each: no honest winner
    assert where.rank([a, {"unit_price": "", "amount": 1.0}]) == (None, False)


def test_a_per_package_count_price_is_not_compared():
    """Codex review 2026-09-27: Whole Foods quotes 64 oz milk as "$6.29/count"
    at $6.29 — a price per bottle. Against a 128 oz bottle at "$8/count" the
    smaller one would have won though it costs more per ounce."""
    small = {"unit_price": "$6.29/count", "amount": 6.29}
    big = {"unit_price": "$8.00/count", "amount": 8.0}
    assert where.rank([small, big]) == (None, False)
    # a real per-item price inside a multi-pack still compares...
    bag5 = {"unit_price": "$0.30/each", "amount": 1.49}
    bag4 = {"unit_price": "$0.45/each", "amount": 1.79}
    assert where.rank([bag5, bag4]) == (bag5, True)
    # ...but a single loose item's "each" looks exactly like a whole bottle's
    # "count" (unit price = shelf price), so it is conservatively not ranked
    loose = {"unit_price": "$0.50 each", "amount": 0.5}
    assert where.rank([bag5, loose]) == (None, False)


# --- the endpoint ---------------------------------------------------------------

def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    r = client.post("/api/op", json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def stores():
    ids = {}
    for name, num in (("Where A", "901"), ("Where B", "902")):
        op(type="store_upsert", store_name=name, store_chain="wegmans", store_chain_id=num)
    for s in client.get("/api/state").json()["stores"]:
        if s["name"] in ("Where A", "Where B"):
            ids[s["name"]] = s["id"]
    return ids


def stub(monkeypatch, by_store, fail=()):
    """by_store: {store_number: {term: [records]}}; stores in `fail` cannot be asked."""
    calls = []

    async def fake(chain, terms, store, prefer=None, max_age=None, place=True):
        calls.append({"store": store, "terms": list(terms), "max_age": max_age, "place": place})
        if store in fail:
            return {}, False
        return {t: by_store.get(store, {}).get(t, []) for t in terms}, True

    monkeypatch.setattr(lookup_api, "products_many", fake)
    return calls


def add(name, **edit):
    res = op(type="add", name=name, item_id=str(uuid.uuid4()))
    if edit:
        op(type="edit", item_id=res["item_id"], **edit)
    return res["catalog_id"]


def test_where_names_the_cheapest_per_unit_and_never_saves_it(monkeypatch, stores):
    cid = add("where butter")
    calls = stub(monkeypatch, {
        "901": {"where butter": [rec("Butter 8oz", 3.0, "$0.38/oz")]},
        "902": {"where butter": [rec("Butter 16oz", 5.0, "$0.31/oz")]},
    })
    d = client.post("/api/where", json={"catalog_ids": [cid]}).json()
    it = d["items"][str(cid)]
    assert it["comparable"] and it["cheapest"]["store"] == "Where B"      # cheaper per ounce
    assert [q["store"] for q in it["quotes"]][:2] == ["Where A", "Where B"]  # listed by amount
    assert all(c["place"] is False and c["max_age"] == lookup.TTL["price"] for c in calls)
    row = appmod.conn.execute("SELECT preferred_store_id FROM item_catalog WHERE id=?", (cid,)).fetchone()
    assert row["preferred_store_id"] is None                               # a view, not a preference


@pytest.fixture
def organic_on():
    op(type="settings", organic=True)
    yield
    op(type="settings", organic=False)


def test_where_searches_organic_and_filters_brand(monkeypatch, stores, organic_on):
    cid = add("where milk", brand="Horizon")
    calls = stub(monkeypatch, {
        "901": {"organic where milk": [rec("Organic Milk", 4.0, "$0.03/fl oz", brand="Store"),
                                       rec("Horizon Organic Milk", 5.0, "$0.04/fl oz", brand="Horizon")]},
        "902": {"organic where milk": [rec("Horizon Milk", 3.0, "$0.02/fl oz", brand="Horizon")]},  # not organic
    })
    d = client.post("/api/where", json={"catalog_ids": [cid]}).json()
    it = d["items"][str(cid)]
    assert any("organic where milk" in c["terms"] for c in calls)
    assert [q["product"] for q in it["quotes"]] == ["Horizon Organic Milk"]
    assert it["stores"][str(stores["Where B"])] == "no_match"


def test_no_match_is_only_claimed_when_every_store_answered(monkeypatch, stores):
    cid = add("where saffron")
    stub(monkeypatch, {"901": {"where saffron": []}}, fail={"902"})
    d = client.post("/api/where", json={"catalog_ids": [cid]}).json()
    it = d["items"][str(cid)]
    assert d["partial"] is True and it["reason"] == "unasked"
    assert it["stores"][str(stores["Where B"])] == "unasked"


def test_where_is_a_503_when_nothing_could_be_asked(monkeypatch, stores):
    cid = add("where cumin")

    async def down(chain, terms, store, prefer=None, max_age=None, place=True):
        return {}, False

    monkeypatch.setattr(lookup_api, "products_many", down)
    assert client.post("/api/where", json={"catalog_ids": [cid]}).status_code == 503


def test_where_caps_the_request(stores):
    ids = list(range(1, lookup_api.WHERE_MAX_ITEMS + 2))
    assert client.post("/api/where", json={"catalog_ids": ids}).status_code == 422


def test_where_is_refused_when_prices_are_switched_off(monkeypatch, stores):
    monkeypatch.setattr(lookup, "MODE", "stores")
    r = client.post("/api/where", json={"catalog_ids": [add("where salt")]})
    assert r.status_code == 503 and r.json()["detail"]["code"] == lookup.DISABLED


def test_with_organic_on_an_item_nobody_sells_organic_falls_back(monkeypatch, stores, organic_on):
    """Paper towels do not come organic: with the household setting on, an item
    no store carries organic is priced as the regular product, and says so."""
    cid = add("where towels")
    calls = stub(monkeypatch, {
        "901": {"organic where towels": [], "where towels": [rec("Bounty Towels", 9.0, "$0.05/sq ft")]},
        "902": {"organic where towels": [], "where towels": [rec("Viva Towels", 7.0, "$0.04/sq ft")]},
    })
    it = client.post("/api/where", json={"catalog_ids": [cid]}).json()["items"][str(cid)]
    assert it["organic_fallback"] is True
    assert {q["product"] for q in it["quotes"]} == {"Bounty Towels", "Viva Towels"}
    assert any("where towels" in c["terms"] for c in calls)


def test_no_fallback_while_a_store_could_not_be_asked(monkeypatch, stores, organic_on):
    """An unreachable store might have had it organic: do not settle for
    conventional on a partial answer."""
    cid = add("where oats")
    stub(monkeypatch, {"901": {"organic where oats": []}}, fail={"902"})
    it = client.post("/api/where", json={"catalog_ids": [cid]}).json()["items"][str(cid)]
    assert it["organic_fallback"] is False and it["reason"] == "unasked"
