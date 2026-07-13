"""Skip-op semantics + typo verification (user feedback 2026-07-03), ported to
multi-tenancy: authed requests; snooze now lives in catalog_snooze (per
household), not on the shared item_catalog row."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import auth_hdr, register_household

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402

client = TestClient(appmod.app)
TOKEN, HH, _UID = register_household(client, "skip")
H = auth_hdr(TOKEN)
HID = HH["id"]


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body, headers=H)


def fake(coro_result):
    async def _f(*a, **kw):
        return coro_result
    return _f


def test_skip_removes_without_event_and_short_snoozes():
    iid = str(uuid.uuid4())
    op(type="add", name="バター", item_id=iid)
    _, res = op(type="skip", item_id=iid)
    assert res.status_code == 200 and "skipped" in res.json()["result"]
    # off the list
    assert all(i["name"] != "バター"
               for i in client.get("/api/state", headers=H).json()["items"])
    # NO purchase event (out-of-stock must not pollute intervals)
    n = appmod.conn.execute(
        """SELECT COUNT(*) FROM purchase_events e JOIN item_catalog c
           ON c.id=e.catalog_id WHERE c.canonical_name=? AND e.household_id=?""",
        (db.canonical("バター"), HID)).fetchone()[0]
    assert n == 0
    # snoozed for ~1 day, in the HOUSEHOLD's snooze table (multi-tenant move)
    until = appmod.conn.execute(
        """SELECT s.snoozed_until FROM catalog_snooze s JOIN item_catalog c
           ON c.id = s.catalog_id
           WHERE c.canonical_name=? AND s.household_id=?""",
        (db.canonical("バター"), HID)).fetchone()[0]
    delta = datetime.fromisoformat(until) - datetime.now(timezone.utc)
    assert timedelta(hours=20) < delta <= timedelta(hours=25)


def test_skip_of_missing_item_is_noop():
    _, res = op(type="skip", item_id=str(uuid.uuid4()))
    assert res.json()["result"] == {"noop": True}


def test_typo_suspect_hidden_from_candidates(monkeypatch):
    monkeypatch.setattr(catalog, "web_evidence", fake(["no real hits"]))
    monkeypatch.setattr(llm, "chat_json", fake(
        {"is_real_item": False, "category": "other", "is_edible": False,
         "plants": [], "alias_of": None, "english_name": None}))
    iid = str(uuid.uuid4())
    op(type="add", name="ぎゅうにゅうう", item_id=iid)  # typo'd milk
    cid = appmod.conn.execute(
        "SELECT catalog_id FROM items WHERE id=?", (iid,)).fetchone()[0]
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, cid))
    # still on the list (the user typed it, it must stay shoppable) …
    assert any(i["id"] == iid
               for i in client.get("/api/state", headers=H).json()["items"])
    # … but never offered as a typing candidate
    names = {c["name"] for c in client.get("/api/catalog", headers=H).json()["catalog"]}
    assert "ぎゅうにゅうう" not in names


def test_real_item_verified_and_gets_english_alias(monkeypatch):
    monkeypatch.setattr(catalog, "web_evidence", fake(["ほうじ茶とは", "ほうじ茶 人気"]))
    monkeypatch.setattr(llm, "chat_json", fake(
        {"is_real_item": True, "category": "drinks", "is_edible": True,
         "plants": ["tea"], "alias_of": None, "english_name": "Roasted green tea"}))
    iid = str(uuid.uuid4())
    op(type="add", name="ほうじ茶", item_id=iid)
    cid = appmod.conn.execute(
        "SELECT catalog_id FROM items WHERE id=?", (iid,)).fetchone()[0]
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, cid))
    entry = [c for c in client.get("/api/catalog", headers=H).json()["catalog"]
             if c["name"] == "ほうじ茶"]
    assert entry and entry[0]["name_en"] == "roasted green tea"
    # state exposes name_en for EN display mode
    item = [i for i in client.get("/api/state", headers=H).json()["items"]
            if i["id"] == iid][0]
    assert item["name_en"] == "roasted green tea"
