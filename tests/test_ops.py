"""Sync-op contract tests — the PLAN.md Phase 0 invariants, exercised via the API.

Every test hits POST /api/op through TestClient; nothing is mocked below the HTTP
layer, so these prove the real dedupe/idempotency/undo behavior end to end.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ["PLANTCART_DB"] = str(
    Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"plantcart_test_{uuid.uuid4().hex}.db"
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    res = client.post("/api/op", json=body)
    return body, res


def events(name):
    canon = db.canonical(name)
    return appmod.conn.execute(
        """SELECT e.* FROM purchase_events e JOIN item_catalog c ON c.id=e.catalog_id
           WHERE c.canonical_name=?""",
        (canon,),
    ).fetchall()


def items():
    return {i["name"]: i for i in client.get("/api/state").json()["items"]}


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
    r1 = client.post("/api/op", json=body)
    r2 = client.post("/api/op", json=body)  # replay, byte-identical
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

    hist = client.get("/api/history").json()["history"]
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


def test_undo_purchase_replay_is_idempotent():
    """Double-tap 'Not bought' on the same row must delete ONE event, not error."""
    iid = str(uuid.uuid4())
    op(type="add", name="okra", item_id=iid)
    op(type="checkoff", item_id=iid)
    event_id = [h for h in client.get("/api/history").json()["history"]
                if h["name"] == "okra"][0]["event_id"]
    body, _ = op(type="undo_purchase", event_id=event_id)
    # replay the SAME op_id → re-ACKed from the ledger, no double effect
    replay = client.post("/api/op", json=body)
    assert replay.json()["replayed"] is True
    # a DIFFERENT op targeting the now-deleted event → clean no-op
    _, res2 = op(type="undo_purchase", event_id=event_id)
    assert res2.json()["result"] == {"noop": True}
    assert len(events("okra")) == 0


def test_revision_monotonic_and_replay_does_not_bump():
    r0 = client.get("/api/state").json()["revision"]
    iid = str(uuid.uuid4())
    body = {"op_id": str(uuid.uuid4()), "actor": "t", "type": "add",
            "name": f"unique-{iid[:8]}", "item_id": iid}
    client.post("/api/op", json=body)
    r1 = client.get("/api/state").json()["revision"]
    client.post("/api/op", json=body)  # replay
    r2 = client.get("/api/state").json()["revision"]
    assert r1 == r0 + 1 and r2 == r1


def test_websocket_broadcasts_after_op():
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()  # full state on connect
        assert "items" in first and "revision" in first
        op(type="add", name="broccoli", item_id=str(uuid.uuid4()))
        pushed = ws.receive_json()
        assert any(i["name"] == "broccoli" for i in pushed["items"])
        assert pushed["revision"] > first["revision"]


def test_rejects_garbage():
    assert client.post("/api/op", json={"op_id": "x" * 10, "type": "add"}).status_code == 422
    assert client.post("/api/op", json={"op_id": "short", "type": "add", "name": "x"}).status_code == 422


def test_suggestions_and_snooze_flow():
    """Seed a weekly history via SQL, expect a suggestion; snooze hides it for both."""
    from datetime import datetime, timedelta, timezone

    cid = db.get_or_create_catalog(appmod.conn, "bananas")
    t0 = datetime.now(timezone.utc) - timedelta(days=27)
    for d in (0, 7, 14, 21):  # last buy 6 days ago → due score ~0.86
        appmod.conn.execute(
            "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)",
            (cid, (t0 + timedelta(days=d)).isoformat(timespec="seconds")),
        )
    appmod.conn.commit()

    sugg = client.get("/api/state").json()["suggestions"]
    mine = [s for s in sugg if s["catalog_id"] == cid]
    assert mine and mine[0]["label"] == "weekly"

    # adding it to the list removes the suggestion
    iid = str(uuid.uuid4())
    op(type="add", name="bananas", item_id=iid)
    assert not [s for s in client.get("/api/state").json()["suggestions"]
                if s["catalog_id"] == cid]
    op(type="remove", item_id=iid)  # back off the list → suggestion returns
    assert [s for s in client.get("/api/state").json()["suggestions"]
            if s["catalog_id"] == cid]

    # snooze silences it (server-side → both phones)
    _, res = op(type="snooze", catalog_id=cid)
    assert "snoozed_until" in res.json()["result"]
    assert not [s for s in client.get("/api/state").json()["suggestions"]
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
                "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)",
                (cid, (t0 - timedelta(days=since + interval * (3 - k))).isoformat(timespec="seconds")))
    appmod.conn.commit()

    rows = {c["name"]: c for c in client.get("/api/cycles").json()["cycles"]}
    assert rows["cyc_overdue"]["due"] and rows["cyc_overdue"]["label"] == "weekly"
    assert not rows["cyc_fresh"]["due"]      # bought yesterday
    assert not rows["cyc_lapsed"]["due"]     # retired, but still visible in the list
    scores = [c["score"] for c in client.get("/api/cycles").json()["cycles"]]
    assert scores == sorted(scores, reverse=True)
    # an item currently on the list is flagged and not due
    iid = str(uuid.uuid4())
    op(type="add", name="cyc_overdue", item_id=iid)
    row = [c for c in client.get("/api/cycles").json()["cycles"] if c["name"] == "cyc_overdue"][0]
    assert row["on_list"] and not row["due"]
    op(type="remove", item_id=iid)
