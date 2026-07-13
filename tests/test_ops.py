"""Sync-op contract tests — the PLAN.md Phase 0 invariants, exercised via the API.

Ported from master @5566205 (16 tests) to the multi-tenant API: every request is
authenticated, everything runs inside ONE registered household, and the direct
SQL fixtures carry household_id. Nothing is mocked below the HTTP layer.
"""
import uuid

from fastapi.testclient import TestClient

from conftest import auth_hdr, register_household

import app as appmod  # noqa: E402
import db  # noqa: E402

client = TestClient(appmod.app)
TOKEN, HH, _UID = register_household(client, "ops")
H = auth_hdr(TOKEN)
HID = HH["id"]


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    res = client.post("/api/op", json=body, headers=H)
    return body, res


def events(name):
    canon = db.canonical(name)
    return appmod.conn.execute(
        """SELECT e.* FROM purchase_events e JOIN item_catalog c ON c.id=e.catalog_id
           WHERE c.canonical_name=? AND e.household_id=?""",
        (canon, HID),
    ).fetchall()


def items():
    return {i["name"]: i for i in client.get("/api/state", headers=H).json()["items"]}


def test_add_and_state():
    _, res = op(type="add", name="Milk", item_id=str(uuid.uuid4()))
    assert res.status_code == 200
    assert "Milk" in items()


def test_add_idempotent_per_canonical_name():
    """Both spouses adding ミルク/ﾐﾙｸ offline must converge to ONE row (NFKC fold)."""
    op(type="add", name="ミルク", item_id=str(uuid.uuid4()))
    _, res = op(type="add", name="ﾐﾙｸ", item_id=str(uuid.uuid4()))
    assert res.json()["result"]["deduped"] is True
    assert sum(1 for n in items() if db.canonical(n) == db.canonical("ミルク")) == 1


def test_op_replay_is_reacked_not_reapplied():
    """Lost ACK → client replays the same op_id → exactly one purchase_event."""
    iid = str(uuid.uuid4())
    op(type="add", name="eggs", item_id=iid)
    body = {"op_id": str(uuid.uuid4()), "actor": "test", "type": "checkoff", "item_id": iid}
    r1 = client.post("/api/op", json=body, headers=H)
    r2 = client.post("/api/op", json=body, headers=H)  # replay, byte-identical
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert len(events("eggs")) == 1
    assert "eggs" not in items()


def test_checkoff_logs_event_and_removes():
    iid = str(uuid.uuid4())
    op(type="add", name="bread", item_id=iid)
    op(type="checkoff", item_id=iid)
    assert len(events("bread")) == 1
    assert "bread" not in items()


def test_remove_logs_no_event():
    """Long-press remove = changed your mind, must NOT pollute frequency data."""
    iid = str(uuid.uuid4())
    op(type="add", name="natto", item_id=iid)
    op(type="remove", item_id=iid)
    assert len(events("natto")) == 0
    assert "natto" not in items()


def test_checkoff_vs_remove_race_is_noop():
    """Phone A checks off while phone B removes: second op lands as no-op ACK."""
    iid = str(uuid.uuid4())
    op(type="add", name="tofu", item_id=iid)
    op(type="checkoff", item_id=iid)
    _, res = op(type="remove", item_id=iid)  # loser of the race
    assert res.status_code == 200
    assert res.json()["result"] == {"noop": True}
    assert len(events("tofu")) == 1  # the purchase survived


def test_undo_checkoff_deletes_event_and_restores_item():
    iid = str(uuid.uuid4())
    op(type="add", name="yogurt", item_id=iid, qty_note="2 packs")
    co_body, _ = op(type="checkoff", item_id=iid)
    assert len(events("yogurt")) == 1
    op(type="undo_checkoff", target_op_id=co_body["op_id"])
    assert len(events("yogurt")) == 0  # fat-finger must not poison intervals
    assert items()["yogurt"]["qty_note"] == "2 packs"  # snapshot restored


def test_undo_of_unknown_op_is_noop():
    _, res = op(type="undo_checkoff", target_op_id=str(uuid.uuid4()))
    assert res.json()["result"] == {"noop": True}


def test_history_lists_recent_and_undo_purchase_repairs_mis_swipe():
    """History panel: a checkoff appears in /api/history; undo_purchase (keyed by
    the server event id, not the op ledger) deletes it and restores the item."""
    iid = str(uuid.uuid4())
    op(type="add", name="edamame", item_id=iid)
    op(type="checkoff", item_id=iid)
    assert len(events("edamame")) == 1
    assert "edamame" not in items()

    hist = client.get("/api/history", headers=H).json()["history"]
    mine = [h for h in hist if h["name"] == "edamame"]
    assert len(mine) == 1
    event_id = mine[0]["event_id"]

    _, res = op(type="undo_purchase", event_id=event_id)
    assert res.status_code == 200
    assert len(events("edamame")) == 0        # spurious purchase gone from the intervals
    assert "edamame" in items()               # and back on the list to re-buy


def test_undo_purchase_unknown_event_is_noop():
    _, res = op(type="undo_purchase", event_id=999999999)
    assert res.json()["result"] == {"noop": True}


def test_undo_purchase_cross_household_is_noop():
    """NEW vs master: household B must not be able to undo A's purchase — a
    foreign event id is indistinguishable from a nonexistent one."""
    iid = str(uuid.uuid4())
    op(type="add", name="cross-hh-item", item_id=iid)
    op(type="checkoff", item_id=iid)
    event_id = [h for h in client.get("/api/history", headers=H).json()["history"]
                if h["name"] == "cross-hh-item"][0]["event_id"]

    tok_b, _, _ = register_household(client, "intruder")
    r = client.post("/api/op", json={
        "op_id": str(uuid.uuid4()), "actor": "b",
        "type": "undo_purchase", "event_id": event_id,
    }, headers=auth_hdr(tok_b))
    assert r.status_code == 200
    assert r.json()["result"] == {"noop": True}
    assert len(events("cross-hh-item")) == 1  # A's event untouched


def test_undo_purchase_replay_is_idempotent():
    """Double-tap 'Not bought' on the same row must delete ONE event, not error."""
    iid = str(uuid.uuid4())
    op(type="add", name="okra", item_id=iid)
    op(type="checkoff", item_id=iid)
    event_id = [h for h in client.get("/api/history", headers=H).json()["history"]
                if h["name"] == "okra"][0]["event_id"]
    body, _ = op(type="undo_purchase", event_id=event_id)
    # replay the SAME op_id → re-ACKed from the ledger, no double effect
    replay = client.post("/api/op", json=body, headers=H)
    assert replay.json()["replayed"] is True
    # a DIFFERENT op targeting the now-deleted event → clean no-op
    _, res2 = op(type="undo_purchase", event_id=event_id)
    assert res2.json()["result"] == {"noop": True}
    assert len(events("okra")) == 0


def test_edit_qty_and_category_override():
    """Ported for multi-tenancy: qty edits the household's item; category lands
    in catalog_category_override, never the shared item_catalog row."""
    iid = str(uuid.uuid4())
    op(type="add", name="edit-target", item_id=iid)
    _, res = op(type="edit", item_id=iid, qty_note="3 bags", category="pantry")
    assert res.status_code == 200 and res.json()["result"]["changed"] is True
    assert items()["edit-target"]["qty_note"] == "3 bags"
    assert items()["edit-target"]["category"] == "pantry"
    shared = appmod.conn.execute(
        "SELECT c.category FROM item_catalog c WHERE c.display_name='edit-target'"
    ).fetchone()
    assert shared["category"] is None  # the corpus row is untouched

    # household B sees the corpus default, not A's preference
    tok_b, _, _ = register_household(client, "other")
    hb = auth_hdr(tok_b)
    client.post("/api/op", json={"op_id": str(uuid.uuid4()), "actor": "b",
                                 "type": "add", "name": "edit-target",
                                 "item_id": str(uuid.uuid4())}, headers=hb)
    b_items = {i["name"]: i for i in
               client.get("/api/state", headers=hb).json()["items"]}
    assert b_items["edit-target"]["category"] is None


def test_revision_monotonic_and_replay_does_not_bump():
    r0 = client.get("/api/state", headers=H).json()["revision"]
    iid = str(uuid.uuid4())
    body = {"op_id": str(uuid.uuid4()), "actor": "t", "type": "add",
            "name": f"unique-{iid[:8]}", "item_id": iid}
    client.post("/api/op", json=body, headers=H)
    r1 = client.get("/api/state", headers=H).json()["revision"]
    client.post("/api/op", json=body, headers=H)  # replay
    r2 = client.get("/api/state", headers=H).json()["revision"]
    assert r1 == r0 + 1 and r2 == r1


def test_websocket_broadcasts_after_op():
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        first = ws.receive_json()  # full state on connect
        assert "items" in first and "revision" in first
        op(type="add", name="broccoli", item_id=str(uuid.uuid4()))
        pushed = ws.receive_json()
        assert any(i["name"] == "broccoli" for i in pushed["items"])
        assert pushed["revision"] > first["revision"]


def test_rejects_garbage():
    assert client.post("/api/op", json={"op_id": "x" * 10, "type": "add"},
                       headers=H).status_code == 422
    assert client.post("/api/op", json={"op_id": "short", "type": "add", "name": "x"},
                       headers=H).status_code == 422


def test_suggestions_and_snooze_flow():
    """Seed a weekly history via SQL, expect a suggestion; snooze hides it for both."""
    from datetime import datetime, timedelta, timezone

    cid = db.get_or_create_catalog(appmod.conn, "bananas")
    t0 = datetime.now(timezone.utc) - timedelta(days=27)
    for d in (0, 7, 14, 21):  # last buy 6 days ago → due score ~0.86
        appmod.conn.execute(
            "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
            (HID, cid, (t0 + timedelta(days=d)).isoformat(timespec="seconds")),
        )
    appmod.conn.commit()

    sugg = client.get("/api/state", headers=H).json()["suggestions"]
    mine = [s for s in sugg if s["catalog_id"] == cid]
    assert mine and mine[0]["label"] == "weekly"

    # adding it to the list removes the suggestion
    iid = str(uuid.uuid4())
    op(type="add", name="bananas", item_id=iid)
    assert not [s for s in client.get("/api/state", headers=H).json()["suggestions"]
                if s["catalog_id"] == cid]
    op(type="remove", item_id=iid)  # back off the list → suggestion returns
    assert [s for s in client.get("/api/state", headers=H).json()["suggestions"]
            if s["catalog_id"] == cid]

    # snooze silences it (server-side → both phones)
    _, res = op(type="snooze", catalog_id=cid)
    assert "snoozed_until" in res.json()["result"]
    assert not [s for s in client.get("/api/state", headers=H).json()["suggestions"]
                if s["catalog_id"] == cid]


def test_cycles_endpoint_full_list():
    """/api/cycles: every learned cycle, most-due first, due/on_list flags."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime.now(timezone.utc)
    fixtures = {"cyc_overdue": (7, 9), "cyc_fresh": (7, 1), "cyc_lapsed": (7, 100)}
    for name, (interval, since) in fixtures.items():
        cid = db.get_or_create_catalog(appmod.conn, name)
        for k in range(4):
            appmod.conn.execute(
                "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
                (HID, cid,
                 (t0 - timedelta(days=since + interval * (3 - k))).isoformat(timespec="seconds")))
    appmod.conn.commit()

    rows = {c["name"]: c for c in client.get("/api/cycles", headers=H).json()["cycles"]}
    assert rows["cyc_overdue"]["due"] and rows["cyc_overdue"]["label"] == "weekly"
    assert not rows["cyc_fresh"]["due"]      # bought yesterday
    assert not rows["cyc_lapsed"]["due"]     # retired, but still visible in the list
    scores = [c["score"] for c in client.get("/api/cycles", headers=H).json()["cycles"]]
    assert scores == sorted(scores, reverse=True)
    # an item currently on the list is flagged and not due
    iid = str(uuid.uuid4())
    op(type="add", name="cyc_overdue", item_id=iid)
    row = [c for c in client.get("/api/cycles", headers=H).json()["cycles"]
           if c["name"] == "cyc_overdue"][0]
    assert row["on_list"] and not row["due"]
    op(type="remove", item_id=iid)
