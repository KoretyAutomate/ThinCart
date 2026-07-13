"""Phase 2 tests — enrichment, alias merge, plant counter, ideas endpoint.

Ported from master @5566205 to multi-tenancy: seeds carry household_id, requests
are authenticated, and the ideas tests run on a 'plus'-tier household (basic
gets 402 by design — covered in test_saas_tier below). llm.chat_json is
monkeypatched (deterministic); everything below the LLM call is real.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import auth_hdr, register_household

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402

client = TestClient(appmod.app)
TOKEN, HH, _UID = register_household(client, "plants")
H = auth_hdr(TOKEN)
HID = HH["id"]
# recipes/advice are plus-tier; this module tests the feature, not the gate
appmod.conn.execute("UPDATE households SET tier='plus' WHERE id=?", (HID,))
appmod.conn.commit()


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
        "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
        (HID, cid, ts),
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
        "INSERT INTO items(id, household_id, catalog_id, added_at, revision) VALUES(?,?,?,?,1)",
        (iid, HID, variant, "2026-07-03T00:00:00+00:00"),
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


def test_alias_merge_blocked_across_households(monkeypatch):
    """NEW vs master: a row multiple households already use must never be merged
    away on one tenant's (possibly hallucinated) alias_of."""
    shared = db.get_or_create_catalog(appmod.conn, "共有アイテム")
    target = db.get_or_create_catalog(appmod.conn, "共有あいてむ先")
    appmod.conn.execute(
        "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
        (HID, shared, "2026-07-01T00:00:00+00:00"))
    tok_b, hh_b, _ = register_household(client, "hhB")
    appmod.conn.execute(
        "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
        (hh_b["id"], shared, "2026-07-02T00:00:00+00:00"))
    appmod.conn.commit()
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "produce", "is_edible": True, "plants": [],
         "alias_of": db.canonical("共有あいてむ先")}))
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, shared))
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM item_catalog WHERE id=?", (shared,)).fetchone()[0] == 1
    assert target  # target row also intact


def test_alias_merge_repoints_snoozes_with_pk_coalesce(monkeypatch):
    """Snoozes must survive a merge; a household snoozed on BOTH rows keeps the
    LATER snoozed_until (composite PK would otherwise explode the UPDATE)."""
    survivor = seed_purchase("ミックスナッツ", 3, plants=[])
    doomed = db.get_or_create_catalog(appmod.conn, "みっくすなっつ")
    db.set_snooze(appmod.conn, HID, survivor, "2026-07-20T00:00:00+00:00")
    db.set_snooze(appmod.conn, HID, doomed, "2026-07-25T00:00:00+00:00")
    appmod.conn.commit()
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "pantry", "is_edible": True, "plants": [],
         "alias_of": db.canonical("ミックスナッツ")}))
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, doomed))
    rows = appmod.conn.execute(
        "SELECT catalog_id, snoozed_until FROM catalog_snooze WHERE household_id=?",
        (HID,)).fetchall()
    mine = [r for r in rows if r["catalog_id"] == survivor]
    assert len(mine) == 1 and mine[0]["snoozed_until"] == "2026-07-25T00:00:00+00:00"
    assert not [r for r in rows if r["catalog_id"] == doomed]


def test_alias_merge_blocked_by_category_override(monkeypatch):
    """A household's deliberate category edit pins the row unmergeable —
    master's edit-protects-from-merge invariant, per-tenant."""
    keeper = db.get_or_create_catalog(appmod.conn, "オーバーライド品")
    db.get_or_create_catalog(appmod.conn, "オーバーライド先")
    appmod.conn.execute(
        "INSERT INTO catalog_category_override(household_id, catalog_id, category) "
        "VALUES(?,?,?)", (HID, keeper, "pantry"))
    appmod.conn.commit()
    monkeypatch.setattr(llm, "chat_json", fake_llm(
        {"category": "pantry", "is_edible": True, "plants": [],
         "alias_of": db.canonical("オーバーライド先")}))
    assert asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, keeper))
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM item_catalog WHERE id=?", (keeper,)).fetchone()[0] == 1


def test_weekly_plants_union_and_window():
    seed_purchase("miso", 2, plants=["soybean", "rice"])
    seed_purchase("bread", 4, plants=["wheat"])
    seed_purchase("old kale", 20, plants=["kale"])          # outside 7d window
    seed_purchase("shampoo", 1, plants=[], edible=0)         # non-food contributes none
    week = catalog.weekly_plants(appmod.conn, HID)
    assert {"soybean", "rice", "wheat"} <= set(week)
    assert "kale" not in week
    state = client.get("/api/state", headers=H).json()
    # count is WEIGHTED plant points, not len(week) — see plants.WEIGHTS
    assert state["plants"]["count"] == catalog.weekly_score(appmod.conn, HID)
    assert state["plants"]["target"] == 30


def test_weekly_plants_scoped_per_household():
    """NEW vs master: household B's purchases never inflate A's plant count."""
    tok_b, hh_b, _ = register_household(client, "hhC")
    cid = db.get_or_create_catalog(appmod.conn, "b-only berry")
    appmod.conn.execute(
        "UPDATE item_catalog SET plants_json=?, is_edible=1, llm_enriched_at='x' WHERE id=?",
        (json.dumps(["blueberry"]), cid))
    appmod.conn.execute(
        "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
        (hh_b["id"], cid,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    appmod.conn.commit()
    assert "blueberry" not in catalog.weekly_plants(appmod.conn, HID)
    assert "blueberry" in catalog.weekly_plants(appmod.conn, hh_b["id"])


def test_ideas_endpoint_shapes_and_cache(monkeypatch):
    def respond(prompt):
        if "recipes" in prompt:
            return {"recipes": [{"title": "Miso soup", "uses": ["miso"],
                                 "missing": ["豆腐"], "new_plants": ["seaweed"]}]}
        return {"suggestions": [{"plant": "chard", "buy": "スイスチャード"}]}
    monkeypatch.setattr(llm, "chat_json", fake_llm(respond))
    appmod.ideas_cache.clear()

    data = client.get("/api/ideas?refresh=1", headers=H).json()
    assert data["recipes"][0]["missing"] == ["豆腐"]
    assert data["diversity"][0]["buy"] == "スイスチャード"

    # cached: LLM now "down", same payload still served
    monkeypatch.setattr(llm, "chat_json", fake_llm(None))
    assert client.get("/api/ideas", headers=H).json() == data


def test_ideas_llm_down_returns_503_not_crash(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_llm(None))
    appmod.ideas_cache.clear()
    assert client.get("/api/ideas?refresh=1", headers=H).status_code == 503
    # and the list is unaffected
    assert client.get("/api/state", headers=H).status_code == 200


def test_ideas_basic_tier_gets_402():
    """The paid seam: a basic household is told to upgrade, not errored."""
    tok_b, _, _ = register_household(client, "basicHH")
    assert client.get("/api/ideas", headers=auth_hdr(tok_b)).status_code == 402


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
    appmod.ideas_cache.clear()
    data = client.get("/api/ideas?refresh=1", headers=H).json()
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
