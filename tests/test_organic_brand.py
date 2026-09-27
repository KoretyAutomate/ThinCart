"""Phase 7A/7B: organic is a property of the item, brand a standing preference.

"organic onion" must land on the Onion — same catalog row, same purchase
history — with its organic flag set, instead of becoming a second Onion
(PLAN.md §Phase 7). Names are suffixed per test: the app module and its DB are
shared across this process.
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


def test_typing_organic_lands_on_the_existing_item_with_its_history():
    name = "onion-7a1"
    first = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="checkoff", item_id=first["item_id"])
    res = op(type="add", name=f"organic {name}", item_id=str(uuid.uuid4()))
    assert res["catalog_id"] == catalog_row(name)["id"]  # the same Onion
    assert catalog_row(f"organic {name}") is None       # no second one
    assert state_item(name)["organic"] is True
    n = appmod.conn.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (res["catalog_id"],))
    assert n.fetchone()[0] == 1                          # its history came along


def test_organic_add_onto_a_listed_item_dedupes_and_still_sets_the_flag():
    name = "walnuts-7a2"
    op(type="add", name=name, item_id=str(uuid.uuid4()))
    rev = client.get("/api/state").json()["revision"]
    res = op(type="add", name=f"Organic {name}", item_id=str(uuid.uuid4()))
    assert res["deduped"] is True
    assert state_item(name)["organic"] is True
    assert client.get("/api/state").json()["revision"] > rev  # both phones hear about it


def test_a_new_organic_item_is_created_under_its_base_name():
    res = op(type="add", name="organic kale-7a3", item_id=str(uuid.uuid4()))
    row = appmod.conn.execute("SELECT * FROM item_catalog WHERE id=?", (res["catalog_id"],)).fetchone()
    assert row["display_name"] == "kale-7a3" and row["organic"] == 1


def test_a_plain_add_never_clears_organic():
    name = "rice-7a4"
    op(type="add", name=f"organic {name}", item_id=str(uuid.uuid4()))
    op(type="add", name=name, item_id=str(uuid.uuid4()))
    assert state_item(name)["organic"] is True


def test_edit_toggles_organic_and_sets_brand():
    name = "milk-7b1"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], organic=True, brand="  Horizon ")
    it = state_item(name)
    assert it["organic"] is True and it["brand"] == "Horizon"
    op(type="edit", item_id=added["item_id"], organic=False, brand="")
    it = state_item(name)
    assert it["organic"] is False and it["brand"] == ""


def test_an_old_phones_edit_without_the_new_fields_leaves_them_alone():
    """A phone with ops queued before this release sends neither field."""
    name = "yogurt-7b2"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], organic=True, brand="Fage")
    op(type="edit", item_id=added["item_id"], note="plain")
    it = state_item(name)
    assert it["organic"] is True and it["brand"] == "Fage" and it["note"] == "plain"


def test_renaming_to_an_organic_name_is_the_base_item_organic():
    name = "egg whites-7a5"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    res = op(type="edit", item_id=added["item_id"], name=f"Organic {name}")
    assert "rename_skipped" not in res
    it = state_item(name)
    assert it is not None and it["organic"] is True
    assert catalog_row(f"organic {name}") is None


def test_an_explicit_toggle_beats_a_typed_qualifier_in_the_same_save():
    name = "tofu-7a6"
    added = op(type="add", name=name, item_id=str(uuid.uuid4()))
    op(type="edit", item_id=added["item_id"], name=f"organic {name}", organic=False)
    assert state_item(name)["organic"] is False


def test_brand_is_capped():
    added = op(type="add", name="bread-7b3", item_id=str(uuid.uuid4()))
    body = {"op_id": str(uuid.uuid4()), "type": "edit", "item_id": added["item_id"], "brand": "x" * 61}
    assert client.post("/api/op", json=body).status_code == 422
