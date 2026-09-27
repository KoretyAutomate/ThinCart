"""Phase 7A/7B: one item whatever the qualifier; organic a HOUSEHOLD setting.

"organic onion" must land on the Onion — same catalog row, same purchase
history — instead of becoming a second Onion. Whether the household buys
organic is one shared setting (the owner's call, 2026-09-27), not a per-item
flag; the brand is a standing per-item preference (PLAN.md §Phase 7). Names
are suffixed per test: the app module and its DB are shared across this process.
"""

import os
import sys
import uuid
from pathlib import Path

os.environ["THINCART_DB"] = str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db")
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import pytest
from fastapi.testclient import TestClient

import app as appmod
import db

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    res = client.post("/api/op", json=body)
    assert res.status_code == 200, res.text
    return res.json()["result"]


def state_item(name):
    for i in client.get("/api/state").json()["items"]:
        if db.canonical(i["name"]) == db.canonical(name):
            return i
    return None


def catalog_row(name):
    return appmod.conn.execute(
        "SELECT * FROM item_catalog WHERE canonical_name=?", (db.canonical(name),)
    ).fetchone()


@pytest.mark.parametrize(
    "typed, base, organic",
    [
        ("organic onion", "onion", True),
        ("Organic Walnuts", "Walnuts", True),
        ("ＯＲＧＡＮＩＣ　Egg", "Egg", True),     # full-width is folded first
        ("オーガニック卵", "卵", True),
        ("有機・人参", "人参", True),
        ("有機 米", "米", True),
        ("Organic", "Organic", False),            # only the qualifier: not split
        ("simple mills organic seed flour crackers", "simple mills organic seed flour crackers", False),
        ("organics mix", "organics mix", False),  # a word, not the qualifier
        ("onion", "onion", False),
    ],
)
def test_split_organic(typed, base, organic):
    assert db.split_organic(typed) == (base, organic)


def settings():
    return client.get("/api/state").json()["settings"]


def test_typing_organic_lands_on_the_existing_item_with_its_history():
    name = "onion-7a1"
    first = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="checkoff", item_id=first["item_id"])
    res = op(type="add", name=f"organic {name}", item_id=str(uuid.uuid4()))
    assert res["catalog_id"] == catalog_row(name)["id"]  # the same Onion
    assert catalog_row(f"organic {name}") is None       # no second one
    n = appmod.conn.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (res["catalog_id"],))
    assert n.fetchone()[0] == 1                          # its history came along


def test_organic_add_onto_a_listed_item_dedupes():
    name = "walnuts-7a2"
    op(type="add", name=name, item_id=str(uuid.uuid4()))
    assert op(type="add", name=f"Organic {name}", item_id=str(uuid.uuid4()))["deduped"] is True


def test_a_new_organic_item_is_created_under_its_base_name():
    res = op(type="add", name="organic kale-7a3", item_id=str(uuid.uuid4()))
    row = appmod.conn.execute("SELECT * FROM item_catalog WHERE id=?", (res["catalog_id"],)).fetchone()
    assert row["display_name"] == "kale-7a3"


def test_typing_organic_does_not_flip_the_household_setting():
    before = settings()["organic"]
    op(type="add", name="organic rice-7a4", item_id=str(uuid.uuid4()))
    assert settings()["organic"] == before


def test_renaming_to_an_organic_name_is_the_base_item():
    name = "egg whites-7a5"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    res = op(type="edit", item_id=added["item_id"], name=f"Organic {name}")
    assert "rename_skipped" not in res
    assert state_item(name) is not None
    assert catalog_row(f"organic {name}") is None


def test_organic_is_one_household_setting():
    op(type="settings", organic=True)
    assert settings()["organic"] is True
    rev = client.get("/api/state").json()["revision"]
    assert op(type="settings", organic=True)["changed"] is False     # no-op: no revision bump
    assert client.get("/api/state").json()["revision"] == rev
    op(type="settings", organic=False)
    assert settings()["organic"] is False
    assert "organic" not in state_item_any()                           # no per-item flag any more


def state_item_any():
    op(type="add", name="probe-7a6", item_id=str(uuid.uuid4()))
    return state_item("probe-7a6")


def test_settings_without_a_value_is_refused():
    body = {"op_id": str(uuid.uuid4()), "type": "settings"}
    assert client.post("/api/op", json=body).status_code == 422


def test_edit_sets_and_clears_brand():
    name = "milk-7b1"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], brand="  Horizon ")
    assert state_item(name)["brand"] == "Horizon"
    op(type="edit", item_id=added["item_id"], brand="")
    assert state_item(name)["brand"] == ""


def test_an_old_phones_edit_without_the_brand_field_leaves_it_alone():
    """A phone with ops queued before this release does not send it."""
    name = "yogurt-7b2"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], brand="Fage")
    op(type="edit", item_id=added["item_id"], note="plain")
    it = state_item(name)
    assert it["brand"] == "Fage" and it["note"] == "plain"


def test_an_edit_carrying_the_retired_per_item_organic_field_is_harmless():
    """Phones ran the per-item toggle for a few hours; a queued edit from then
    must not flip the household setting."""
    before = settings()["organic"]
    added = op(type="add", name="tofu-7a7", item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], organic=not before)
    assert settings()["organic"] == before


def test_brand_is_capped():
    added = op(type="add", name="bread-7b3", item_id=str(uuid.uuid4()))
    body = {"op_id": str(uuid.uuid4()), "type": "edit", "item_id": added["item_id"], "brand": "x" * 61}
    assert client.post("/api/op", json=body).status_code == 422
