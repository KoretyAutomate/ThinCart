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
import where
import where_api

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
    for st in client.get("/api/state").json()["stores"]:
        if st["name"] in ("Where A", "Where B"):
            ids[st["name"]] = st["id"]
    return ids


def stub(monkeypatch, by_store, fail=()):
    """by_store: {store_number: {term: [records]}}; stores in `fail` cannot be asked.
    Other priced stores left in the shared test DB answer "nothing"."""
    calls = []

    async def fake(chain, terms, store, max_age=None):
        calls.append({"store": store, "terms": list(terms), "max_age": max_age})
        if store in fail:
            return {}, False
        return {t: by_store.get(store, {}).get(t, []) for t in terms}, True

    monkeypatch.setattr(where_api, "price_products_many", fake)
    return calls


def add(name, food=False, **edit):
    res = op(type="add", name=name, item_id=str(uuid.uuid4()))
    if edit:
        op(type="edit", item_id=res["item_id"], **edit)
    if food:  # what enrichment would have said; organic applies to food only
        appmod.conn.execute("UPDATE item_catalog SET is_edible=1 WHERE id=?", (res["catalog_id"],))
        appmod.conn.commit()
    return res["catalog_id"]


def ask(cid):
    r = client.post("/api/where", json={"catalog_ids": [cid]})
    assert r.status_code == 200, r.text
    return r.json()["items"][str(cid)]


@pytest.fixture
def organic_on():
    op(type="settings", organic=True)
    yield
    op(type="settings", organic=False)


def test_cheapest_per_unit_we_compute_and_it_becomes_the_recommendation(monkeypatch, stores):
    cid = add("quillbutter")
    calls = stub(monkeypatch, {
        "901": {"quillbutter": [rec("Quillbutter Sticks, 8 oz", 3.0, "")]},
        "902": {"quillbutter": [rec("Quillbutter Tub, 16 oz", 5.0, "")]},
    })
    it = ask(cid)
    assert it["comparable"] and it["cheapest"]["store"] == "Where B"      # $0.31/oz beats $0.38/oz
    assert it["cheapest"]["unit_label"] == "$0.31/oz"
    assert all(c["max_age"] == lookup.TTL["price"] for c in calls)
    state = {i["catalog_id"]: i for i in client.get("/api/state").json()["items"]}
    assert state[cid]["store"] == "Where B" and state[cid]["store_source"] == "price"
    assert state[cid]["price"]["unit_label"] == "$0.31/oz"
    row = appmod.conn.execute("SELECT preferred_store_id FROM item_catalog WHERE id=?", (cid,)).fetchone()
    assert row["preferred_store_id"] is None                                # never saved as a preference


def test_the_owners_pick_beats_the_price(monkeypatch, stores):
    cid = add("quillmilk", store="Where A")
    stub(monkeypatch, {"901": {"quillmilk": [rec("Quillmilk 64 fl oz", 6.0, "")]},
                       "902": {"quillmilk": [rec("Quillmilk 64 fl oz", 4.0, "")]}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["store"] == "Where A" and item["store_source"] == "preferred"
    assert item["price"]["store"] == "Where B"                              # still shown beside it
    op(type="edit", catalog_id=cid, store="")                               # "use cheapest"
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["store"] == "Where B" and item["store_source"] == "price"


def test_how_much_i_want_beats_the_huge_bag(monkeypatch, stores):
    """The owner's example: 2 lb of rice, not the 20 lb bag that is cheaper per pound."""
    cid = add("quillrice", buy_qty="2 lb")
    stub(monkeypatch, {"901": {"quillrice": [rec("Quillrice, 20 lb Bag", 20.0, "")]},
                       "902": {"quillrice": [rec("Quillrice, 1 lb", 2.5, "")]}})
    it = ask(cid)
    assert it["dim"] == "weight" and it["cheapest"]["store"] == "Where B"
    assert it["cheapest"]["total_label"] == "2 × 1 lb = $5.00"
    no_qty = add("quillgrain")
    stub(monkeypatch, {"901": {"quillgrain": [rec("Quillgrain, 20 lb Bag", 20.0, "")]},
                       "902": {"quillgrain": [rec("Quillgrain, 1 lb", 2.5, "")]}})
    assert ask(no_qty)["cheapest"]["store"] == "Where A"                    # per unit, without one


def test_an_unreadable_amount_is_flagged_not_guessed(monkeypatch, stores):
    cid = add("quillbeans", buy_qty="a few")
    stub(monkeypatch, {"901": {"quillbeans": [rec("Quillbeans 15 oz", 1.0, "")]}})
    it = ask(cid)
    assert it["buy_qty_ok"] is False and it["dim"] == "weight"              # compared per unit instead


def test_rice_is_not_rice_cakes(monkeypatch, stores):
    cid = add("quillrye")
    stub(monkeypatch, {"901": {"quillrye": [rec("Quillrye Cakes, 4 oz", 0.5, ""),
                                            rec("Quillrye, 2 lb", 4.0, "")]}})
    it = ask(cid)
    assert [q["product"] for q in it["quotes"]] == ["Quillrye, 2 lb"]


def test_each_store_is_represented_by_its_best_candidate_not_its_first(monkeypatch, stores):
    cid = add("quilloats")
    stub(monkeypatch, {"901": {"quilloats": [rec("Quilloats, 16 oz", 4.0, ""), rec("Quilloats, 42 oz", 6.0, "")]}})
    it = ask(cid)
    assert it["cheapest"]["product"] == "Quilloats, 42 oz"


def test_organic_applies_to_food_only(monkeypatch, stores, organic_on):
    """Paper towels were searched as "organic paper towels" and found nothing."""
    towels = add("quilltowels")                                             # not food
    calls = stub(monkeypatch, {"901": {"quilltowels": [rec("Quilltowels, 6 Rolls, 110 Sheets Per Roll", 9.0, "")]}})
    it = ask(towels)
    assert it["cheapest"] and not any("organic" in t for c in calls for t in c["terms"])
    tea = add("quilltea", food=True)
    calls = stub(monkeypatch, {"901": {"organic quilltea": [rec("Organic Quilltea 20 ct", 4.0, "")]}})
    assert ask(tea)["cheapest"] and any("organic quilltea" in c["terms"] for c in calls)


def test_with_organic_on_food_nobody_sells_organic_falls_back(monkeypatch, stores, organic_on):
    cid = add("quillsalt", food=True)
    stub(monkeypatch, {"901": {"organic quillsalt": [], "quillsalt": [rec("Quillsalt 26 oz", 2.0, "")]},
                       "902": {"organic quillsalt": [], "quillsalt": [rec("Quillsalt 26 oz", 3.0, "")]}})
    it = ask(cid)
    assert it["organic_fallback"] is True and it["cheapest"]["store"] == "Where A"


def test_no_fallback_while_a_store_could_not_be_asked(monkeypatch, stores, organic_on):
    cid = add("quilloat", food=True)
    stub(monkeypatch, {"901": {"organic quilloat": []}}, fail={"902"})
    it = ask(cid)
    assert it["organic_fallback"] is False and it["reason"] == "unasked"


def test_a_saved_conventional_pick_is_a_conflict_not_a_fallback(monkeypatch, stores, organic_on):
    cid = add("quillyogurt", food=True)
    appmod.conn.execute(
        "INSERT INTO product_picks(catalog_id, chain, sku, name, brand, pack_size, picked_at) "
        "VALUES(?, 'wegmans', 'CONV', 'Plain Quillyogurt', '', '', '2026-09-27')", (cid,))
    appmod.conn.commit()
    stub(monkeypatch, {s: {"Plain Quillyogurt": [rec("Plain Quillyogurt 32 oz", 3.0, "", sku="CONV")]}
                       for s in ("901", "902")})
    it = ask(cid)
    assert it["organic_fallback"] is False and it["quotes"] == [] and it["reason"] == "conflict"


def test_a_failed_refresh_keeps_the_last_answer_a_definitive_miss_drops_it(monkeypatch, stores):
    cid = add("quillpasta")
    stub(monkeypatch, {"901": {"quillpasta": [rec("Quillpasta 16 oz", 2.0, "")]}})
    ask(cid)
    stub(monkeypatch, {}, fail={"901", "902"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] and item["price"]["store"] == "Where A"            # kept, with its age
    stub(monkeypatch, {"901": {"quillpasta": []}, "902": {"quillpasta": []}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] is None and item["store_source"] != "price"


def test_an_edit_hides_a_stale_answer_until_asked_again(monkeypatch, stores):
    cid = add("quillflour")
    stub(monkeypatch, {"901": {"quillflour": [rec("Quillflour 5 lb", 4.0, "")]}})
    ask(cid)
    op(type="edit", catalog_id=cid, buy_qty="2 lb")                         # a different question now
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] is None


def test_a_slow_answer_cannot_overwrite_a_newer_question():
    import price_reco
    cid = add("quillcorn")
    old = price_reco.input_key(appmod.conn, cid)
    op(type="edit", catalog_id=cid, brand="Acme")
    answer = {"cheapest": {"store_id": 1, "amount": 1.0, "product": "x"}}
    assert price_reco.save(appmod.conn, cid, old, answer, "2026-10-03T00:00:00+00:00") is False


def test_where_is_a_503_when_nothing_could_be_asked(monkeypatch, stores):
    cid = add("quillcumin")
    stub(monkeypatch, {}, fail={"901", "902"})

    async def down(chain, terms, store, max_age=None):
        return {}, False

    monkeypatch.setattr(where_api, "price_products_many", down)
    assert client.post("/api/where", json={"catalog_ids": [cid]}).status_code == 503


def test_where_caps_the_request(stores):
    ids = list(range(1, where_api.WHERE_MAX_ITEMS + 2))
    assert client.post("/api/where", json={"catalog_ids": ids}).status_code == 422


def test_where_is_refused_when_prices_are_switched_off(monkeypatch, stores):
    monkeypatch.setattr(lookup, "MODE", "stores")
    r = client.post("/api/where", json={"catalog_ids": [add("quillsugar")]})
    assert r.status_code == 503 and r.json()["detail"]["code"] == lookup.DISABLED


def test_the_brand_goes_into_the_search(monkeypatch, stores):
    cid = add("quillcream", brand="Horizon")
    calls = stub(monkeypatch, {})
    client.post("/api/where", json={"catalog_ids": [cid]})
    assert any("Horizon quillcream" in c["terms"] for c in calls)


def test_a_definitive_answer_without_a_winner_clears_the_old_one(monkeypatch, stores):
    """Codex review: the picked product went out of stock (conflict) and the
    stale recommendation stayed in /api/state."""
    cid = add("quillhoney")
    stub(monkeypatch, {"901": {"quillhoney": [rec("Quillhoney 12 oz", 4.0, "")]}})
    ask(cid)
    stub(monkeypatch, {"901": {"quillhoney": [rec("Quillhoney 12 oz", 4.0, "", available=False)]},
                       "902": {"quillhoney": []}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] is None


def test_weight_and_volume_stay_apart_except_for_liquids(monkeypatch, stores):
    cid = add("quillsyrup")                                                  # not a liquid word
    stub(monkeypatch, {"901": {"quillsyrup": [rec("Quillsyrup 12 oz", 4.0, "")]},
                       "902": {"quillsyrup": [rec("Quillsyrup 12 fl oz", 3.0, "")]}})
    it = ask(cid)
    assert it["dim"] == "weight" and it["cheapest"]["store"] == "Where A"   # the fl oz one is not compared
    milk = add("quill milk")
    stub(monkeypatch, {"901": {"quill milk": [rec("Quill Milk 59 oz", 5.0, "")]},
                       "902": {"quill milk": [rec("Quill Milk 64 fl oz", 6.0, "")]}})
    it = ask(milk)
    assert it["comparable"] and {q["store"] for q in it["quotes"]} == {"Where A", "Where B"}


def test_a_percentage_names_the_product():
    """Codex review: '2% milk' and '1% milk' were the same words."""
    assert where.relevant("Wegmans 2% Reduced Fat Milk, 1 gal", "grass fed 2% milk") is False  # grass fed missing
    assert where.relevant("Grass Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Grass Fed 1% Milk, 64 fl oz", "grass fed 2% milk") is False


def test_a_partial_refresh_does_not_replace_a_complete_answer(monkeypatch, stores):
    """Codex review: the cheapest store unreachable on refresh let the next one
    take its place as 'cheapest' with no warning."""
    cid = add("quillpeas")
    stub(monkeypatch, {"901": {"quillpeas": [rec("Quillpeas 16 oz", 2.0, "")]},
                       "902": {"quillpeas": [rec("Quillpeas 16 oz", 5.0, "")]}})
    ask(cid)
    stub(monkeypatch, {"902": {"quillpeas": [rec("Quillpeas 16 oz", 5.0, "")]}}, fail={"901"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"]["store"] == "Where A" and item["price"]["partial"] is False


def test_a_store_moved_to_another_chain_is_a_new_question(stores):
    import price_reco
    cid = add("quillchain")
    before = price_reco.input_key(appmod.conn, cid)
    appmod.conn.execute("UPDATE stores SET chain='shoprite' WHERE id=?", (stores["Where A"],))
    appmod.conn.commit()
    try:
        assert price_reco.input_key(appmod.conn, cid) != before
    finally:
        appmod.conn.execute("UPDATE stores SET chain='wegmans' WHERE id=?", (stores["Where A"],))
        appmod.conn.commit()


def test_a_winner_its_own_store_contradicts_is_dropped_even_on_a_partial_refresh(monkeypatch, stores):
    """Codex review: A had been cheapest; A now says its product is gone while
    B is unreachable — A's old recommendation must not stand."""
    cid = add("quilllentils")
    stub(monkeypatch, {"901": {"quilllentils": [rec("Quilllentils 16 oz", 2.0, "")]},
                       "902": {"quilllentils": [rec("Quilllentils 16 oz", 5.0, "")]}})
    ask(cid)
    stub(monkeypatch, {"901": {"quilllentils": [rec("Quilllentils 16 oz", 2.0, "", available=False)]}},
         fail={"902"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] is None


def test_the_price_age_is_the_fetch_not_the_comparison(monkeypatch, stores):
    cid = add("quillbarley")
    old = rec("Quillbarley 16 oz", 2.0, "")
    old["fetched_at"] = "2026-10-01T00:00:00+00:00"
    stub(monkeypatch, {"901": {"quillbarley": [old]}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"]["fetched_at"] == "2026-10-01T00:00:00+00:00"


def test_relevance_checks_the_whole_leading_phrase_and_full_width_terms():
    """Codex review: 'Rice Cakes, Brown Rice' passed for brown rice, and a
    full-width '２％ milk' skipped every check."""
    assert where.relevant("Rice Cakes, Brown Rice, 16 oz", "brown rice") is False
    assert where.relevant("Lundberg Brown Rice, 2 lb", "brown rice") is True
    assert where.relevant("Honey Roasted Peanuts, 16 oz", "peanuts") is True
    assert where.relevant("1% Milk, 64 fl oz", "２％ milk") is False
    assert where.relevant("2% Milk, 64 fl oz", "２％ milk") is True


def test_excluded_product_types_are_folded_like_product_words():
    """Codex review: 'cookies' singular-folds to 'cooky' and slipped past."""
    assert where.relevant("Rice Cookies, 8 oz", "rice") is False
    assert where.relevant("Fruit Gummies, 6 oz", "fruit") is False


def test_found_organic_replaces_a_conventional_stand_in(monkeypatch, stores, organic_on):
    """Codex review: the stored answer was a regular product (nobody had it
    organic); A is now unreachable but B has organic — B's organic wins."""
    cid = add("quillkale", food=True)
    stub(monkeypatch, {"901": {"organic quillkale": [], "quillkale": [rec("Quillkale 16 oz", 2.0, "")]},
                       "902": {"organic quillkale": [], "quillkale": [rec("Quillkale 16 oz", 3.0, "")]}})
    assert ask(cid)["organic_fallback"] is True
    stub(monkeypatch, {"902": {"organic quillkale": [rec("Organic Quillkale 16 oz", 4.0, "")]}}, fail={"901"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"]["store"] == "Where B" and "Organic" in item["price"]["product"]


def test_the_item_must_lead_the_product_not_trail_it():
    """Codex review: cached 'organic lemon' results let lemon juices in when
    lemon only appeared after the first comma."""
    assert where.relevant("Simply Lemonade, made with real lemon", "lemon") is False
    assert where.relevant("Wegmans Organic Lemons, 2 lb", "organic lemon") is True
    # a store-brand lead segment is skipped, not mistaken for the product
    assert where.relevant("365 By Whole Foods Market, Tofu Firm Organic, 14 Ounce", "firm tofu",
                          "365 By Whole Foods Market") is True


def test_ingredients_after_with_do_not_change_the_product():
    """Codex review: a cached body lotion was rejected for 'with ... Coconut Oil'."""
    assert where.relevant("Shea Moisture Daily Hydration Body Lotion with Virgin Coconut Oil 16oz",
                          "body lotion") is True
    assert where.relevant("Wegmans Body Lotion Oil Blend", "body lotion") is False
    # and a match found only in the ingredient clause is not the item (Codex review)
    assert where.relevant("Body Lotion with Virgin Coconut Oil, 16 fl oz", "coconut oil") is False
    assert where.relevant("Nutiva Organic Coconut Oil, 15 fl oz", "coconut oil") is True


def test_a_conventional_stand_in_is_kept_until_its_regular_product_is_checked(monkeypatch, stores, organic_on):
    """Codex review: A's stand-in was dropped when A said 'no organic' while B
    was unreachable — the regular search never ran, so A never contradicted it."""
    cid = add("quillchard", food=True)
    stub(monkeypatch, {"901": {"organic quillchard": [], "quillchard": [rec("Quillchard 16 oz", 2.0, "")]},
                       "902": {"organic quillchard": [], "quillchard": [rec("Quillchard 16 oz", 3.0, "")]}})
    assert ask(cid)["organic_fallback"] is True
    stub(monkeypatch, {"901": {"organic quillchard": []}}, fail={"902"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] and item["price"]["store"] == "Where A"


def test_organic_found_but_unrankable_still_ends_the_stand_in(monkeypatch, stores, organic_on):
    """Codex review: organic kale sold by the bunch could not be ranked against
    the stand-in sold by weight, so the conventional recommendation stayed."""
    cid = add("quillcollard", food=True)
    stub(monkeypatch, {"901": {"organic quillcollard": [], "quillcollard": [rec("Quillcollard 16 oz", 2.0, "")]},
                       "902": {"organic quillcollard": [], "quillcollard": [rec("Quillcollard 16 oz", 3.0, "")]}})
    assert ask(cid)["organic_fallback"] is True
    stub(monkeypatch, {"901": {"organic quillcollard": [rec("Organic Quillcollard, 1 Bunch", 3.0, "")]},
                       "902": {"organic quillcollard": []}})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"] is None or "Organic" in item["price"]["product"]


def test_cached_dill_relish_is_not_dill():
    """Codex review: the cached Whole Foods 'organic dill' results put relish first."""
    assert where.relevant("Organic Dill Relish, 10 oz", "organic dill") is False
    assert where.relevant("McCormick Gourmet Collection Organic Dill Weed, 0.5 oz", "organic dill") is True


def test_joined_and_separated_spellings_match():
    """Codex review: every cached Wegmans oat milk was rejected for 'oat milk'."""
    assert where.relevant("Wegmans Original Oatmilk, 64 fl oz", "oat milk") is True
    assert where.relevant("Oat Milk Barista, 32 fl oz", "oatmilk") is True
    assert where.relevant("Grassfed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Oatmilk Creamer, 32 fl oz", "oat milk") is True       # creamer is not excluded
    assert where.relevant("Oat Cereal, 12 oz", "oat milk") is False


def test_brand_and_ingredient_words_do_not_name_the_item():
    """Codex review, cached products: 'Whole' only in the store brand, and
    'Pumpkin' only among a cat treat's ingredients."""
    assert where.relevant("365 By Whole Foods Market, Organic Baby Carrots, 2 lb", "whole carrot",
                          "365 By Whole Foods Market") is False
    assert where.relevant("Fancy Feast Savory Purees with Chicken & Pumpkin Cat Treats", "pumpkin puree") is False
    # a variety written after the comma still counts: Wegmans writes it that way
    assert where.relevant("Wegmans Organic Squash, Butternut", "butternut squash", "Wegmans") is True


def test_a_hyphenated_descriptor_is_not_an_ingredient_clause():
    """Codex review: 'Stir-In' split into 'stir' + 'in' cut the phrase before 'paste'."""
    assert where.relevant("Wegmans Organic Dill Stir-In Paste", "dill", "Wegmans") is False
    assert where.relevant("Grass-Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True


def test_a_size_in_the_item_name_is_not_a_product_word():
    """Found on cached data: 'salmon 2 lb' rejected every salmon fillet."""
    assert where.relevant("Sockeye Salmon Fillet, 32 oz", "organic salmon 2 lb") is True
    assert where.relevant("365 by Whole Foods Market Sockeye Salmon Fillets, 10 OZ", "salmon 2 lb",
                          "365 by Whole Foods Market") is True


def test_a_unit_word_is_a_size_only_after_a_number():
    """Codex review: 'eggs' was stripped as a count unit, leaving no words —
    so egg noodles passed for eggs."""
    assert where.relevant("Egg Noodles, 12 oz", "eggs") is False
    assert where.relevant("Large Brown Eggs, 12 ct", "eggs") is True
    assert where.relevant("Sockeye Salmon Fillet, 32 oz", "salmon 2 lb") is True


def test_brands_inline_and_brands_named_by_the_item():
    """Codex review, cached ShopRite: 'Pumpkin Tree' fruit puree is not
    pumpkin puree; 'Daisy, Sour Cream' is a Daisy sour cream."""
    assert where.relevant("Pumpkin Tree Strawberry & Banana Fruit Puree, 4 oz", "pumpkin puree",
                          "Pumpkin Tree") is False
    assert where.relevant("Farmer's Market Organic Pumpkin Puree, 15 oz", "pumpkin puree", "Farmer's Market") is True
    assert where.relevant("Daisy, Sour Cream, 16 Ounce", "Daisy sour cream", "Daisy") is True
    assert where.relevant("Breakstone's Sour Cream, 16 oz", "Daisy sour cream", "Breakstone's") is False


def test_whole_size_phrases_leave_the_item_name():
    """Codex review: 'milk 64 fl oz' kept 'fl' and 'oz' as required words."""
    assert where.relevant("Milk, 64 Fluid Ounces", "milk 64 fl oz") is True
    assert where.relevant("Paper Towels, 6 Rolls", "paper towels 600 sq ft") is True


def test_an_ingredient_list_further_on_does_not_name_the_item():
    """Codex review, cached: a baby puree listing pumpkin among its flavours."""
    assert where.relevant("Cerebelly Baby Puree, Organic, White Bean, Pumpkin, Apple with Cinnamon",
                          "pumpkin puree", "Cerebelly") is False
    assert where.relevant("Wegmans Organic Squash, Butternut", "butternut squash", "Wegmans") is True


def test_the_organic_stand_in_warning_survives_into_state(monkeypatch, stores, organic_on):
    cid = add("quillsorrel", food=True)
    stub(monkeypatch, {"901": {"organic quillsorrel": [], "quillsorrel": [rec("Quillsorrel 4 oz", 2.0, "")]},
                       "902": {"organic quillsorrel": [], "quillsorrel": [rec("Quillsorrel 4 oz", 3.0, "")]}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"]["organic_fallback"] is True


def test_descriptors_in_later_segments_still_count_and_frosting_is_not_cheese():
    """Codex review, cached: SoyBoy and Organic Valley put descriptors in the
    third segment; Duncan Hines frosting is not whipped cream cheese."""
    assert where.relevant("SoyBoy Tofu, Organic, Extra Firm", "extra firm tofu", "SoyBoy") is True
    assert where.relevant("Organic Valley Cheese Slices, Non-Smoked, Provolone", "cheese slices provolone",
                          "Organic Valley") is True
    assert where.relevant("Duncan Hines Whipped Cream Cheese Frosting", "whipped cream cheese", "Duncan Hines") is False
