"""Skip-op semantics + typo verification (user feedback 2026-07-03)."""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body)


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
    assert all(i["name"] != "バター" for i in client.get("/api/state").json()["items"])
    # NO purchase event (out-of-stock must not pollute intervals)
    n = appmod.conn.execute(
        """SELECT COUNT(*) FROM purchase_events e JOIN item_catalog c
           ON c.id=e.catalog_id WHERE c.canonical_name=?""",
        (db.canonical("バター"),)).fetchone()[0]
    assert n == 0
    # snoozed for ~1 day (re-suggested next trip, not silenced for half a cycle)
    until = appmod.conn.execute(
        "SELECT snoozed_until FROM item_catalog WHERE canonical_name=?",
        (db.canonical("バター"),)).fetchone()[0]
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
    assert any(i["id"] == iid for i in client.get("/api/state").json()["items"])
    # … but never offered as a typing candidate
    names = {c["name"] for c in client.get("/api/catalog").json()["catalog"]}
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
    entry = [c for c in client.get("/api/catalog").json()["catalog"]
             if c["name"] == "ほうじ茶"]
    assert entry and entry[0]["name_en"] == "roasted green tea"
    # state exposes name_en for EN display mode
    item = [i for i in client.get("/api/state").json()["items"] if i["id"] == iid][0]
    assert item["name_en"] == "roasted green tea"
