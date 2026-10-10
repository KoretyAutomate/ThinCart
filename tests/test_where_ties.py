"""Price ties: the same lowest price at more than one store (server/where.py,
where_api._persist, db.price_pick). Shares the endpoint helpers of test_where."""

import uuid

import test_where as tw  # first: it points THINCART_DB at a throwaway file before app loads
import where_api
from test_where import add, ask, client, op, rec, stub

stores = tw.stores   # the fixture, shared


def test_a_tie_names_every_store_at_the_lowest_price(monkeypatch, stores):
    cid = add("quilltie")
    stub(monkeypatch, {"901": {"quilltie": [rec("Quilltie 16 oz", 4.0, "")]},
                       "902": {"quilltie": [rec("Quilltie 16 oz", 4.0, "")]}})
    it = ask(cid)
    assert it["cheapest"]["store"] in ("Where A", "Where B")
    assert [t["store"] for t in it["tied"]] == [({"Where A", "Where B"} - {it["cheapest"]["store"]}).pop()]
    state = {i["catalog_id"]: i for i in client.get("/api/state").json()["items"]}
    pr = state[cid]["price"]
    assert sorted([pr["store"], *pr["also"]]) == ["Where A", "Where B"]


def test_no_tie_when_a_store_is_even_a_cent_dearer(monkeypatch, stores):
    cid = add("quillnotie")
    stub(monkeypatch, {"901": {"quillnotie": [rec("Quillnotie 16 oz", 4.0, "")]},
                       "902": {"quillnotie": [rec("Quillnotie 16 oz", 4.01, "")]}})
    it = ask(cid)
    assert it["cheapest"]["store"] == "Where A" and it["tied"] == []


def test_a_tie_on_the_wanted_amount_counts_the_cost_not_the_pack(monkeypatch, stores):
    cid = add("quillpack", buy_qty="2 lb")
    stub(monkeypatch, {"901": {"quillpack": [rec("Quillpack, 1 lb", 2.5, "")]},
                       "902": {"quillpack": [rec("Quillpack, 2 lb", 5.0, "")]}})
    it = ask(cid)
    assert len(it["tied"]) == 1                                # 2 × 1 lb = $5.00 = 2 lb = $5.00


def test_the_store_you_already_buy_at_wins_the_tie(monkeypatch, stores):
    cid = add("quillhabit")
    iid = op(type="add", name="quillhabit", item_id=str(uuid.uuid4()))["item_id"]
    op(type="checkoff", item_id=iid, store="Where B")          # bought at B before
    add("quillhabit")
    stub(monkeypatch, {"901": {"quillhabit": [rec("Quillhabit 8 oz", 3.0, "")]},
                       "902": {"quillhabit": [rec("Quillhabit 8 oz", 3.0, "")]}})
    ask(cid)
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["store"] == "Where B" and item["price"]["also"] == ["Where A"]


def test_a_refresh_that_disproves_part_of_a_tie_drops_only_that_store(monkeypatch, stores):
    """Codex review: A and B tied at $4; now A cannot be asked and B costs $8.
    A may still be $4, so it stays; B is no longer a winner and no tie."""
    cid = add("quilltwin")
    iid = op(type="add", name="quilltwin", item_id=str(uuid.uuid4()))["item_id"]
    op(type="checkoff", item_id=iid, store="Where B")              # history favours B
    add("quilltwin")
    stub(monkeypatch, {"901": {"quilltwin": [rec("Quilltwin 8 oz", 4.0, "")]},
                       "902": {"quilltwin": [rec("Quilltwin 8 oz", 4.0, "")]}})
    ask(cid)
    stub(monkeypatch, {"902": {"quilltwin": [rec("Quilltwin 8 oz", 8.0, "")]}}, fail={"901"})
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    assert item["price"]["store"] == "Where A" and item["price"]["also"] == []


def _twin(monkeypatch, name, second):
    """A and B tied at $4; a refresh then cannot ask A and B answers `second`."""
    cid = add(name)
    stub(monkeypatch, {"901": {name: [rec(f"{name} 8 oz", 4.0, "")]}, "902": {name: [rec(f"{name} 8 oz", 4.0, "")]}})
    ask(cid)
    stub(monkeypatch, {"902": {name: [rec(f"{name} {second}", 0, "")]}}, fail={"901"})
    return cid


def _after(cid):
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    return {item["price"]["store"], *item["price"]["also"]}


def test_a_tie_the_reachable_store_confirms_stays(monkeypatch, stores):
    cid = _twin(monkeypatch, "quillconfirm", "8 oz")
    monkeypatch.setattr(where_api, "price_products_many", _fixed(monkeypatch, "quillconfirm", 4.0, "8 oz"))
    assert _after(cid) == {"Where A", "Where B"}


def test_a_reachable_store_in_a_different_pack_size_is_judged_per_unit(monkeypatch, stores):
    cid = _twin(monkeypatch, "quillsize", "4 oz")
    monkeypatch.setattr(where_api, "price_products_many", _fixed(monkeypatch, "quillsize", 4.0, "4 oz"))
    assert _after(cid) == {"Where A"}                     # $1.00/oz is no longer $0.50/oz


def test_a_reachable_store_that_undercuts_the_unreachable_one_wins(monkeypatch, stores):
    cid = _twin(monkeypatch, "quillunder", "8 oz")
    monkeypatch.setattr(where_api, "price_products_many", _fixed(monkeypatch, "quillunder", 2.0, "8 oz"))
    assert _after(cid) == {"Where B"}


def _fixed(monkeypatch, name, amount, size):
    async def fake(chain, terms, store, max_age=None):
        if store == "901":
            return {}, False
        return {t: [rec(f"{name} {size}", amount, "")] if store == "902" else [] for t in terms}, True
    return fake


def test_a_surviving_tie_still_saves_the_fresh_quote_and_check(monkeypatch, stores):
    cid = _twin(monkeypatch, "quillrefresh", "8 oz")
    monkeypatch.setattr(where_api, "price_products_many", _fixed(monkeypatch, "quillrefresh", 8.0, "16 oz"))
    client.post("/api/where", json={"catalog_ids": [cid]})
    item = next(i for i in client.get("/api/state").json()["items"] if i["catalog_id"] == cid)
    names = {item["price"]["store"], *item["price"]["also"]}
    assert names == {"Where A", "Where B"}
    assert item["price"]["partial"] is True                 # this check could not reach A
    if item["price"]["store"] == "Where B":
        assert item["price"]["amount"] == 8.0               # B's new package, not the old $4
