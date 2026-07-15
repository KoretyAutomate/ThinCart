"""Specificity + editor tests (2026-07-12 user report):
- name_en never lets a banked generic alias shadow an English-typed name
- the _is_variety backstop keeps 'white rice' / 'fettuccine pasta' out of the
  generic row even if the LLM calls them an alias
- the alias merge still collapses TRUE script-variant aliases
- the long-press editor's `edit` op adjusts quantity + category
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body)


def items():
    return {i["name"]: i for i in client.get("/api/state").json()["items"]}


# ── name_en: the user's typed English name always wins ──────────────────────
def test_name_en_keeps_english_display_over_generic_alias():
    # even if 'rice' got banked as an alias, 'White rice' must display as typed
    assert db.name_en('["rice"]', "White rice") == "White rice"
    assert db.name_en('["bagel"]', "One Mighty Mill bagel") == "One Mighty Mill bagel"


def test_name_en_uses_alias_only_for_japanese_display():
    assert db.name_en('["onion"]', "玉ねぎ") == "onion"   # JP item → English alias
    assert db.name_en('[]', "玉ねぎ") is None              # no alias → client shows JA


# ── _is_variety: deterministic backstop against variety/brand merges ────────
def test_is_variety_blocks_qualifier_supersets():
    assert catalog._is_variety("white rice", ["rice"]) is True
    assert catalog._is_variety("fettuccine pasta", ["pasta"]) is True
    assert catalog._is_variety("green bell pepper", ["bell pepper"]) is True


def test_is_variety_allows_true_aliases_and_distinct_words():
    assert catalog._is_variety("spaghetti", ["pasta"]) is False       # distinct word
    assert catalog._is_variety("yellow squash", ["zucchini"]) is False
    assert catalog._is_variety("rice", ["rice"]) is False             # identical, not superset
    assert catalog._is_variety("玉ねぎ", ["たまねぎ"]) is False


# ── enrich(): guard blocks variety merge, true script-variant still merges ──
def _run(coro):
    return asyncio.run(coro)


async def _enrich_with_fake_llm(monkeypatch_val, source, target_canon, alias_of):
    async def fake_web(_name):
        return None

    async def fake_chat(prompt, **kw):
        return {"is_real_item": True, "category": "pantry", "is_edible": 1,
                "plants": ["wheat"], "english_name": source, "alias_of": alias_of}

    catalog.web_evidence = fake_web
    catalog.llm.chat_json = fake_chat
    lock = asyncio.Lock()
    sid = db.get_or_create_catalog(appmod.conn, source)   # fresh, mergeable
    await catalog.enrich(appmod.conn, lock, sid)
    return sid


def test_enrich_blocks_variety_merge(monkeypatch):
    orig_web, orig_chat = catalog.web_evidence, catalog.llm.chat_json
    try:
        tgt = db.get_or_create_catalog(appmod.conn, "rice-generic-xyz")
        appmod.conn.execute(
            "UPDATE item_catalog SET category='pantry', llm_enriched_at='2026-01-01' WHERE id=?",
            (tgt,))
        appmod.conn.commit()
        sid = _run(_enrich_with_fake_llm(None, "white rice-generic-xyz",
                                         "rice-generic-xyz", "rice-generic-xyz"))
        # guard should have refused the merge → the source row still exists
        assert appmod.conn.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE id=?", (sid,)).fetchone()[0] == 1
    finally:
        catalog.web_evidence, catalog.llm.chat_json = orig_web, orig_chat


def test_enrich_still_merges_true_script_alias(monkeypatch):
    orig_web, orig_chat = catalog.web_evidence, catalog.llm.chat_json
    try:
        tgt = db.get_or_create_catalog(appmod.conn, "玉ねぎテスト")
        appmod.conn.execute(
            "UPDATE item_catalog SET category='produce', llm_enriched_at='2026-01-01' WHERE id=?",
            (tgt,))
        appmod.conn.commit()
        sid = _run(_enrich_with_fake_llm(None, "たまねぎテスト", "玉ねぎテスト", "玉ねぎテスト"))
        # true script variant → merged away (source row deleted, folded into target)
        assert appmod.conn.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE id=?", (sid,)).fetchone()[0] == 0
        aliases = json.loads(appmod.conn.execute(
            "SELECT aliases_json FROM item_catalog WHERE id=?", (tgt,)).fetchone()[0])
        assert "たまねぎテスト" in aliases
    finally:
        catalog.web_evidence, catalog.llm.chat_json = orig_web, orig_chat


# ── edit op (long-press editor) ─────────────────────────────────────────────
def test_edit_updates_quantity_and_category():
    iid = str(uuid.uuid4())
    op(type="add", name="edit-target-abc", item_id=iid)
    _, res = op(type="edit", item_id=iid, qty_note="3 boxes", category="frozen")
    assert res.status_code == 200 and res.json()["result"]["changed"] is True
    it = items()["edit-target-abc"]
    assert it["qty_note"] == "3 boxes"
    assert it["category"] == "frozen"


def test_edit_rejects_invalid_category():
    iid = str(uuid.uuid4())
    op(type="add", name="edit-badcat-abc", item_id=iid)
    _, res = op(type="edit", item_id=iid, category="not_a_category")
    assert res.status_code == 422


def test_edit_unknown_item_is_noop():
    _, res = op(type="edit", item_id=str(uuid.uuid4()), qty_note="x")
    assert res.status_code == 200 and res.json()["result"] == {"noop": True}
