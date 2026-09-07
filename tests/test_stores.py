"""Phase 5: stores, notes/purchase criteria, where-to-buy recommendation.

Same style as test_ops.py — everything through POST /api/op + /api/state, so
these prove the real op contract (idempotency, criteria persistence, the
preferred>history precedence) end to end.
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

from fastapi.testclient import TestClient

import app as appmod
import db

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    res = client.post("/api/op", json=body)
    return body, res


def state():
    return client.get("/api/state").json()


def items():
    return {i["name"]: i for i in state()["items"]}


def stores_by_name():
    return {s["name"]: s for s in state()["stores"]}


def add(name):
    iid = str(uuid.uuid4())
    op(type="add", name=name, item_id=iid)
    return iid


def test_store_get_or_create_collides_on_canonical():
    """'OK Store' vs 'ok　store' (full-width space) must be ONE row — a plain
    INSERT would 500 on the UNIQUE constraint and wedge the client op queue."""
    a = db.get_or_create_store(appmod.conn, "OK Store")
    b = db.get_or_create_store(appmod.conn, "ok　store")
    assert a == b
    assert db.get_or_create_store(appmod.conn, "") is None


def test_edit_sets_note_budget_store():
    iid = add("洗剤")
    _, res = op(type="edit", item_id=iid, note="無香料のやつ", budget="３００円", store="OKストア")
    assert res.status_code == 200
    it = items()["洗剤"]
    assert it["note"] == "無香料のやつ"
    assert it["budget"] == 300.0  # full-width digits + 円 parsed
    assert it["store"] == "OKストア"
    assert it["store_source"] == "preferred"
    assert "OKストア" in stores_by_name()  # auto-created


def test_budget_clear_and_invalid():
    iid = add("budget-item")
    op(type="edit", item_id=iid, budget="¥1,200")
    assert items()["budget-item"]["budget"] == 1200.0
    op(type="edit", item_id=iid, budget="abc")  # invalid → ignored, not 422
    assert items()["budget-item"]["budget"] == 1200.0
    op(type="edit", item_id=iid, budget="")  # "" clears
    assert items()["budget-item"]["budget"] is None


def test_store_clear_via_empty_string():
    iid = add("clear-store-item")
    op(type="edit", item_id=iid, store="Store X")
    assert items()["clear-store-item"]["store"] == "Store X"
    op(type="edit", item_id=iid, store="")
    it = items()["clear-store-item"]
    assert it["store"] is None and it["store_source"] is None


def test_checkoff_stamps_store_and_recommends_from_history():
    """'I'm at:' stamping is the ground truth behind the recommendation."""
    iid = add("stamped")
    op(type="checkoff", item_id=iid, store="Costco")
    sid = state()["stores"]  # store exists
    assert any(s["name"] == "Costco" for s in sid)
    ev = appmod.conn.execute(
        """SELECT e.store_id FROM purchase_events e
           JOIN item_catalog c ON c.id=e.catalog_id WHERE c.canonical_name=?""",
        (db.canonical("stamped"),),
    ).fetchone()
    assert ev["store_id"] is not None
    add("stamped")  # back on the list → history-based recommendation
    it = items()["stamped"]
    assert it["store"] == "Costco" and it["store_source"] == "history"


def test_preferred_beats_history():
    iid = add("pref-item")
    op(type="checkoff", item_id=iid, store="History Mart")
    iid2 = add("pref-item")
    op(type="edit", item_id=iid2, store="Pinned Mart")
    it = items()["pref-item"]
    assert it["store"] == "Pinned Mart" and it["store_source"] == "preferred"


def test_history_recommendation_prefers_most_frequent():
    for store in ("A-Mart", "B-Mart", "B-Mart"):
        iid = add("freq-item")
        op(type="checkoff", item_id=iid, store=store)
    add("freq-item")
    it = items()["freq-item"]
    assert it["store"] == "B-Mart" and it["store_source"] == "history"


def test_history_tie_breaks_on_the_most_recent_shop():
    """The other half of the documented rule — "tie: most recently" — which
    nothing exercised until now. It is the case that actually happens: two shops
    you use about equally, and the useful answer is where you last bought it,
    not whichever store row was created first.

    The recommendation leans on a subtlety worth pinning: `recommended_stores`
    orders ASCENDING by (count, last) and lets dict overwrite pick the winner,
    so the final row for a catalog_id is the most frequent and, among equals,
    the most recent. A change to that ORDER BY silently inverts the answer.
    """
    for store in ("Old-Mart", "New-Mart"):
        iid = add("tie-item")
        op(type="checkoff", item_id=iid, store=store)

    # Equal counts (1 each). Force Old-Mart's purchase to be the older one so
    # recency, not insertion order, is what decides.
    old_id = appmod.conn.execute("SELECT id FROM stores WHERE canonical_name=?",
                                 (db.canonical("Old-Mart"),)).fetchone()["id"]
    appmod.conn.execute(
        """UPDATE purchase_events SET bought_at='2020-01-01T00:00:00+00:00'
           WHERE store_id=? AND catalog_id=(SELECT id FROM item_catalog WHERE canonical_name=?)""",
        (old_id, db.canonical("tie-item")),
    )
    appmod.conn.commit()

    rec = db.recommended_stores(appmod.conn)
    cid = appmod.conn.execute("SELECT id FROM item_catalog WHERE canonical_name=?",
                              (db.canonical("tie-item"),)).fetchone()["id"]
    sid, source = rec[cid]
    name = appmod.conn.execute("SELECT name FROM stores WHERE id=?", (sid,)).fetchone()["name"]
    assert (name, source) == ("New-Mart", "history")


def test_frequency_outranks_recency():
    """Recency only breaks a TIE. A shop used three times beats one visited
    yesterday — otherwise a single unusual trip would rewrite the regular
    answer, which is the opposite of what purchase history is for."""
    for store in ("Regular", "Regular", "Regular"):
        iid = add("freq-beats-recent")
        op(type="checkoff", item_id=iid, store=store)
    iid = add("freq-beats-recent")
    op(type="checkoff", item_id=iid, store="One-Off")   # most recent, count 1

    rec = db.recommended_stores(appmod.conn)
    cid = appmod.conn.execute("SELECT id FROM item_catalog WHERE canonical_name=?",
                              (db.canonical("freq-beats-recent"),)).fetchone()["id"]
    name = appmod.conn.execute("SELECT name FROM stores WHERE id=?",
                               (rec[cid][0],)).fetchone()["name"]
    assert name == "Regular"


def test_a_deleted_store_stops_being_recommended():
    """Deleting a store nulls its purchase references (Phase 5 review delta 7).
    The recommendation must follow — a suggestion naming a shop the household
    has removed is worse than no suggestion."""
    iid = add("gone-item")
    op(type="checkoff", item_id=iid, store="Doomed Mart")
    sid = next(s["id"] for s in state()["stores"] if s["name"] == "Doomed Mart")
    add("gone-item")
    assert items()["gone-item"]["store"] == "Doomed Mart"

    op(type="store_delete", store_id=sid)
    it = items()["gone-item"]
    assert it["store"] is None and it["store_source"] is None


def test_edit_survives_vanished_item_via_catalog_id():
    """Spouse checks the item off while the edit sheet is open — catalog-level
    criteria must still land (their whole point is surviving checkoffs)."""
    iid = add("vanish-item")
    cid = items()["vanish-item"]["catalog_id"]
    op(type="checkoff", item_id=iid)  # item row gone
    _, res = op(type="edit", item_id=iid, catalog_id=cid, note="always the big pack", qty_note="2")
    assert res.status_code == 200
    add("vanish-item")
    it = items()["vanish-item"]
    assert it["note"] == "always the big pack"
    assert it["qty_note"] == ""  # per-item field needed the live row → dropped


def test_note_and_budget_persist_across_checkoff_readd():
    iid = add("persist-item")
    op(type="edit", item_id=iid, note="brand X only", budget="500")
    op(type="checkoff", item_id=iid)
    add("persist-item")
    it = items()["persist-item"]
    assert it["note"] == "brand X only" and it["budget"] == 500.0


def test_store_upsert_creates_and_updates_notes():
    _, res = op(type="store_upsert", store_name="Notes Mart", store_notes="cheap fish")
    assert res.status_code == 200
    assert stores_by_name()["Notes Mart"]["notes"] == "cheap fish"
    op(type="store_upsert", store_name="notes mart", store_notes="cheap fish, good bread")
    s = stores_by_name()["Notes Mart"]  # canonical match → same row, display kept
    assert s["notes"] == "cheap fish, good bread"
    _, bad = op(type="store_upsert", store_notes="no name")
    assert bad.status_code == 422


def test_store_delete_nulls_references():
    iid = add("del-store-item")
    op(type="checkoff", item_id=iid, store="Doomed Mart")
    iid2 = add("del-store-item")
    op(type="edit", item_id=iid2, store="Doomed Mart")
    sid = stores_by_name()["Doomed Mart"]["id"]
    _, res = op(type="store_delete", store_id=sid)
    assert res.status_code == 200
    assert "Doomed Mart" not in stores_by_name()
    it = items()["del-store-item"]
    assert it["store"] is None and it["store_source"] is None
    orphans = appmod.conn.execute("SELECT COUNT(*) FROM purchase_events WHERE store_id=?", (sid,)).fetchone()[0]
    assert orphans == 0
    # double-delete (other phone) → no-op ACK, not an error
    _, res2 = op(type="store_delete", store_id=sid)
    assert res2.status_code == 200 and res2.json()["result"].get("noop") is True


def test_checkoff_with_store_replay_logs_one_event():
    iid = add("replay-store")
    body = {"op_id": str(uuid.uuid4()), "actor": "test", "type": "checkoff", "item_id": iid, "store": "Replay Mart"}
    r1 = client.post("/api/op", json=body)
    r2 = client.post("/api/op", json=body)
    assert r1.status_code == r2.status_code == 200
    n = appmod.conn.execute(
        """SELECT COUNT(*) FROM purchase_events e
           JOIN item_catalog c ON c.id=e.catalog_id WHERE c.canonical_name=?""",
        (db.canonical("replay-store"),),
    ).fetchone()[0]
    assert n == 1


def test_migration_idempotent_on_existing_db():
    """connect() on an already-migrated DB must not raise (ALTERs are guarded)."""
    conn2 = db.connect(db.DB_PATH)
    cols = {r["name"] for r in conn2.execute("PRAGMA table_info(item_catalog)")}
    assert {"note", "budget", "preferred_store_id"} <= cols
    cols_e = {r["name"] for r in conn2.execute("PRAGMA table_info(purchase_events)")}
    assert "store_id" in cols_e
    conn2.close()
