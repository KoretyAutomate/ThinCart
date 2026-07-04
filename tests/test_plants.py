"""Phase 2 tests — enrichment, alias merge, plant counter, ideas endpoint.

llm.chat_json is monkeypatched (deterministic); the real vLLM JSON path was
verified live separately (see test_results/). Everything below the LLM call is real.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault(
    "PLANTCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"plantcart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402

client = TestClient(appmod.app)


def fake_llm(response):
    async def _fake(prompt, **kw):
        return response(prompt) if callable(response) else response
    return _fake


@pytest.fixture(autouse=True)
def no_web_search(monkeypatch):
    """enrich() consults SearXNG for user-typed rows — keep tests offline."""
    async def _none(name):
        return None
    monkeypatch.setattr(catalog, "web_evidence", _none)


def seed_purchase(name, days_ago, plants=None, edible=1):
    cid = db.get_or_create_catalog(appmod.conn, name)
    appmod.conn.execute(
        "UPDATE item_catalog SET plants_json=?, is_edible=?, llm_enriched_at='x' WHERE id=?",
        (json.dumps(plants or []), edible, cid),
    )
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    appmod.conn.execute(
        "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)", (cid, ts)
    )
    appmod.conn.commit()
    return cid


def test_enrich_updates_catalog(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "pantry", "is_edible": True,
         "plants": ["Wheat", "turmeric", "turmeric", "小麦", "cumin"], "alias_of": None}))
    cid = db.get_or_create_catalog(appmod.conn, "カレールー")
    appmod.conn.commit()
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, cid))
    row = appmod.conn.execute("SELECT * FROM item_catalog WHERE id=?", (cid,)).fetchone()
    # tokens cleaned: lowercased, non-ascii and duplicates dropped
    assert json.loads(row["plants_json"]) == ["wheat", "turmeric", "cumin"]
    assert row["category"] == "pantry" and row["is_edible"] == 1
    assert row["llm_enriched_at"] is not None


def test_alias_merge_repoints_history(monkeypatch):
    """たまねぎ and 玉ねぎ must end up ONE catalog row with merged history."""
    onion = seed_purchase("玉ねぎ", 3, plants=["onion"])
    variant = db.get_or_create_catalog(appmod.conn, "たまねぎ")
    iid = str(uuid.uuid4())
    appmod.conn.execute(
        "INSERT INTO items(id, catalog_id, added_at, revision) VALUES(?,?,?,1)",
        (iid, variant, "2026-07-03T00:00:00+00:00"),
    )
    appmod.conn.commit()
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "produce", "is_edible": True, "plants": ["onion"],
         "alias_of": db.canonical("玉ねぎ")}))
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, variant))
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM item_catalog WHERE id=?", (variant,)).fetchone()[0] == 0
    assert appmod.conn.execute(
        "SELECT catalog_id FROM items WHERE id=?", (iid,)).fetchone()[0] == onion
    aliases = json.loads(appmod.conn.execute(
        "SELECT aliases_json FROM item_catalog WHERE id=?", (onion,)).fetchone()[0])
    assert db.canonical("たまねぎ") in aliases


def test_weekly_plants_union_and_window():
    seed_purchase("miso", 2, plants=["soybean", "rice"])
    seed_purchase("bread", 4, plants=["wheat"])
    seed_purchase("old kale", 20, plants=["kale"])          # outside 7d window
    seed_purchase("shampoo", 1, plants=[], edible=0)         # non-food contributes none
    week = catalog.weekly_plants(appmod.conn)
    assert {"soybean", "rice", "wheat"} <= set(week)
    assert "kale" not in week
    state = client.get("/api/state").json()
    assert state["plants"]["count"] == len(week)
    assert state["plants"]["target"] == 30


def test_ideas_endpoint_shapes_and_cache(monkeypatch):
    def respond(prompt):
        if "recipes" in prompt:
            return {"recipes": [{"title": "Miso soup", "uses": ["miso"],
                                 "missing": ["豆腐"], "new_plants": ["seaweed"]}]}
        return {"suggestions": [{"plant": "chard", "buy": "スイスチャード"}]}
    monkeypatch.setattr(llm, "chat_json", fake_llm(respond))
    appmod.ideas_cache["data"] = None

    data = client.get("/api/ideas?refresh=1").json()
    assert data["recipes"][0]["missing"] == ["豆腐"]
    assert data["diversity"][0]["buy"] == "スイスチャード"

    # cached: LLM now "down", same payload still served
    monkeypatch.setattr(llm, "chat_json", fake_llm(None))
    assert client.get("/api/ideas").json() == data


def test_ideas_llm_down_returns_503_not_crash(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_llm(None))
    appmod.ideas_cache["data"] = None
    assert client.get("/api/ideas?refresh=1").status_code == 503
    # and the list is unaffected
    assert client.get("/api/state").status_code == 200


def test_ideas_filters_already_eaten_plants(monkeypatch):
    """LLM suggesting a just-eaten plant must be dropped server-side."""
    seed_purchase("kale bag", 1, plants=["kale"])
    def respond(prompt):
        if "recipes" in prompt:
            return {"recipes": []}
        return {"suggestions": [{"plant": "kale", "buy": "ケール"},
                                {"plant": "kohlrabi", "buy": "コールラビ"},
                                {"plant": "beet", "buy": ""}]}  # empty buy dropped too
    monkeypatch.setattr(llm, "chat_json", fake_llm(respond))
    appmod.ideas_cache["data"] = None
    data = client.get("/api/ideas?refresh=1").json()
    assert [s["plant"] for s in data["diversity"]] == ["kohlrabi"]


def test_curated_rows_are_never_alias_merged(monkeypatch):
    """Regression (live 2026-07-03): sweep merged seeded ミニトマト into トマト.
    Rows with curated category/aliases must survive an LLM alias_of verdict."""
    tomato = db.get_or_create_catalog(appmod.conn, "トマト")
    mini = db.get_or_create_catalog(appmod.conn, "ミニトマト")
    appmod.conn.execute(
        "UPDATE item_catalog SET category='produce', aliases_json=? WHERE id=?",
        (json.dumps(["プチトマト"], ensure_ascii=False), mini))
    appmod.conn.commit()
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "produce", "is_edible": True, "plants": ["tomato"],
         "alias_of": db.canonical("トマト")}))
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, mini))
    # row survived, got enriched in place, and history was NOT repointed
    row = appmod.conn.execute(
        "SELECT plants_json, llm_enriched_at FROM item_catalog WHERE id=?", (mini,)).fetchone()
    assert row is not None and json.loads(row["plants_json"]) == ["tomato"]
    # bare user-typed variant still merges (the intended path)
    variant = db.get_or_create_catalog(appmod.conn, "とまと")
    appmod.conn.commit()
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, variant))
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM item_catalog WHERE id=?", (variant,)).fetchone()[0] == 0
