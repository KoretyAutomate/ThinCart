"""Sync-op contract tests — the PLAN.md Phase 0 invariants, exercised via the API.

Every test hits POST /api/op through TestClient; nothing is mocked below the HTTP
layer, so these prove the real dedupe/idempotency/undo behavior end to end.
"""
import json
import os
import sys
import uuid
from pathlib import Path


os.environ["THINCART_DB"] = str(
    Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402
from datetime import UTC

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


def test_edit_rename_repoints_catalog_keeps_item():
    """Rename re-points items.catalog_id at the new name's catalog row; the
    item id, qty and the OLD catalog row's name all survive untouched."""
    iid = str(uuid.uuid4())
    op(type="add", name="rename-src", item_id=iid, qty_note="2 packs")
    _, res = op(type="edit", item_id=iid, name="rename-dst")
    assert res.status_code == 200 and res.json()["result"]["changed"] is True
    lst = items()
    assert "rename-src" not in lst and "rename-dst" in lst
    assert lst["rename-dst"]["id"] == iid          # same item, not delete+re-add
    assert lst["rename-dst"]["qty_note"] == "2 packs"
    old = appmod.conn.execute(
        "SELECT display_name FROM item_catalog WHERE canonical_name=?",
        (db.canonical("rename-src"),)).fetchone()
    assert old["display_name"] == "rename-src"     # old concept row untouched


def test_edit_rename_and_qty_in_one_op():
    """The sheet saves name+qty together; both must land on the same item."""
    iid = str(uuid.uuid4())
    op(type="add", name="combo-src", item_id=iid)
    op(type="edit", item_id=iid, name="combo-dst", qty_note="500 g")
    lst = items()
    assert lst["combo-dst"]["id"] == iid and lst["combo-dst"]["qty_note"] == "500 g"


def test_edit_rename_onto_existing_item_merges():
    """Renaming to a name already on the list converges to ONE row (the
    survivor keeps its id; the edited row is dropped; qty follows the save)."""
    keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
    op(type="add", name="merge-keep", item_id=keep)
    op(type="add", name="merge-gone", item_id=gone)
    _, res = op(type="edit", item_id=gone, name="merge-keep", qty_note="x3")
    assert res.json()["result"]["merged_into"] == keep
    lst = items()
    assert "merge-gone" not in lst
    assert lst["merge-keep"]["id"] == keep and lst["merge-keep"]["qty_note"] == "x3"
    rows = appmod.conn.execute(
        "SELECT COUNT(*) AS n FROM items i JOIN item_catalog c ON c.id=i.catalog_id "
        "WHERE c.canonical_name=?", (db.canonical("merge-keep"),)).fetchone()
    assert rows["n"] == 1


def test_edit_rename_criteria_follow_new_concept():
    """Rename + criteria in one save: note/category land on the NEW catalog
    row, not the old concept the item is leaving."""
    iid = str(uuid.uuid4())
    op(type="add", name="crit-src", item_id=iid)
    op(type="edit", item_id=iid, name="crit-dst", note="the organic one",
       category="pantry")
    dst = appmod.conn.execute(
        "SELECT note, category FROM item_catalog WHERE canonical_name=?",
        (db.canonical("crit-dst"),)).fetchone()
    assert dst["note"] == "the organic one" and dst["category"] == "pantry"
    src = appmod.conn.execute(
        "SELECT note FROM item_catalog WHERE canonical_name=?",
        (db.canonical("crit-src"),)).fetchone()
    assert not src["note"]  # old concept untouched


def catalog_id_of(item_id):
    return appmod.conn.execute(
        "SELECT catalog_id FROM items WHERE id=?", (item_id,)).fetchone()["catalog_id"]


def test_edit_rename_case_only_persists():
    """Capitalization fix canonicalizes onto the item's OWN row, so the rename
    branch never fires — display_name must still be corrected, or the next
    fetch silently restores the old spelling."""
    iid = str(uuid.uuid4())
    op(type="add", name="case-fix soup", item_id=iid)
    _, res = op(type="edit", item_id=iid, name="Case-Fix Soup")
    assert res.json()["result"]["changed"] is True
    lst = items()
    assert "Case-Fix Soup" in lst and "case-fix soup" not in lst
    assert lst["Case-Fix Soup"]["id"] == iid   # same item, same catalog row


def test_edit_rename_width_variant_persists():
    """Same for a half-width→full-width respelling (ﾊﾞﾀｰ → バター): NFKC folds
    them together, so this too lands on the item's existing row."""
    iid = str(uuid.uuid4())
    op(type="add", name="ﾊﾞﾀｰ-width", item_id=iid)
    cid = catalog_id_of(iid)
    _, res = op(type="edit", item_id=iid, name="バター-width")
    assert res.json()["result"]["changed"] is True
    assert "バター-width" in items()
    assert catalog_id_of(iid) == cid           # respelling, not a new concept
    assert appmod.conn.execute(
        "SELECT COUNT(*) AS n FROM item_catalog WHERE canonical_name=?",
        (db.canonical("バター-width"),)).fetchone()["n"] == 1


def test_edit_rename_identical_name_reports_no_change():
    """Saving the name unchanged really is a no-op — `changed` must not lie
    in the other direction either."""
    iid = str(uuid.uuid4())
    op(type="add", name="idem-name", item_id=iid)
    _, res = op(type="edit", item_id=iid, name="idem-name")
    assert res.json()["result"]["changed"] is False


def test_edit_rename_case_only_with_qty_in_one_save():
    """The sheet saves name+qty together; the respelling must not swallow the
    qty edit (or vice versa)."""
    iid = str(uuid.uuid4())
    op(type="add", name="combo-case", item_id=iid)
    op(type="edit", item_id=iid, name="Combo-Case", qty_note="1 jar")
    lst = items()
    assert lst["Combo-Case"]["id"] == iid and lst["Combo-Case"]["qty_note"] == "1 jar"


def test_edit_rename_onto_own_alias_is_refused_and_reported():
    """Typing an ALIAS of the item's own catalog row also resolves to that row,
    but it is not a respelling: renaming display_name there would rename the
    shared concept for every item reaching it by any other spelling. Refused —
    and said so in the result, so the client can undo its optimistic text."""
    iid, other = str(uuid.uuid4()), str(uuid.uuid4())
    op(type="add", name="alias-concept", item_id=iid)
    cid = catalog_id_of(iid)
    appmod.conn.execute("UPDATE item_catalog SET aliases_json=? WHERE id=?",
                        (json.dumps(["alias-nickname"]), cid))
    _, shared = op(type="add", name="alias-nickname", item_id=other)
    assert shared.json()["result"]["deduped"] is True   # the alias reaches this row
    _, res = op(type="edit", item_id=iid, name="alias-nickname")
    result = res.json()["result"]
    assert result["rename_skipped"] == "alias"
    assert result["name"] == "alias-concept"   # what the client must show instead
    assert result["changed"] is False
    row = appmod.conn.execute(
        "SELECT display_name, canonical_name FROM item_catalog WHERE id=?",
        (cid,)).fetchone()
    assert row["display_name"] == "alias-concept"   # shared row uncorrupted
    assert row["canonical_name"] == db.canonical("alias-concept")
    assert catalog_id_of(iid) == cid                # item stayed put


def test_edit_rename_to_variant_of_a_different_row_still_repoints():
    """A canonical variant of some OTHER catalog row is a genuine rename, not a
    respelling: it must still take the re-point path."""
    src, dst = str(uuid.uuid4()), str(uuid.uuid4())
    op(type="add", name="variant-src", item_id=src)
    op(type="add", name="Variant-Dst", item_id=dst)
    dst_cid = catalog_id_of(dst)
    op(type="remove", item_id=dst)                  # off the list, row survives
    _, res = op(type="edit", item_id=src, name="variant-dst")
    assert res.json()["result"]["catalog_id"] == dst_cid
    assert catalog_id_of(src) == dst_cid
    assert "Variant-Dst" in items()                 # target row's spelling kept


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
    from datetime import datetime, timedelta

    cid = db.get_or_create_catalog(appmod.conn, "bananas")
    t0 = datetime.now(UTC) - timedelta(days=27)
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
    from datetime import datetime, timedelta

    t0 = datetime.now(UTC)
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
