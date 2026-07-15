"""Typing-candidates backend: seed idempotency, alias matching, /api/catalog."""
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

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402
import seed_catalog  # noqa: E402

client = TestClient(appmod.app)


@pytest.fixture(autouse=True, scope="module")
def cleanup_seeds():
    """The DB (and its module-level connection) is shared across test files —
    remove the seeded rows afterwards so alias matching doesn't leak into
    other files' fixtures ('bread' must not become 食パン elsewhere)."""
    yield
    for name, *_ in seed_catalog.SEED:
        row = appmod.conn.execute(
            "SELECT id FROM item_catalog WHERE canonical_name=?", (db.canonical(name),)
        ).fetchone()
        if row:
            appmod.conn.execute("DELETE FROM items WHERE catalog_id=?", (row["id"],))
            appmod.conn.execute("DELETE FROM purchase_events WHERE catalog_id=?", (row["id"],))
            appmod.conn.execute("DELETE FROM item_catalog WHERE id=?", (row["id"],))
    appmod.conn.commit()


def test_seed_is_idempotent():
    n1 = seed_catalog.seed(appmod.conn)
    n2 = seed_catalog.seed(appmod.conn)
    assert n1 == len(seed_catalog.SEED) and n2 == 0


def test_add_via_english_alias_lands_on_seeded_row():
    """Typing 'milk' or 'たまご' must map onto the seeded 牛乳 / 卵 rows."""
    seed_catalog.seed(appmod.conn)
    milk_id = appmod.conn.execute(
        "SELECT id FROM item_catalog WHERE canonical_name=?", (db.canonical("牛乳"),)
    ).fetchone()[0]
    assert db.get_or_create_catalog(appmod.conn, "Milk") == milk_id
    egg_id = appmod.conn.execute(
        "SELECT id FROM item_catalog WHERE canonical_name=?", (db.canonical("卵"),)
    ).fetchone()[0]
    assert db.get_or_create_catalog(appmod.conn, "たまご") == egg_id
    # a genuinely new name still creates a new row
    new_id = db.get_or_create_catalog(appmod.conn, "ドラゴンフルーツ")
    assert new_id not in (milk_id, egg_id)


def test_api_catalog_shape_and_frequency_order():
    seed_catalog.seed(appmod.conn)
    # buy 食パン twice → it must outrank never-bought seeds
    cid = db.get_or_create_catalog(appmod.conn, "食パン")
    for ts in ("2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00"):
        appmod.conn.execute(
            "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)", (cid, ts))
    appmod.conn.commit()
    cat = client.get("/api/catalog").json()["catalog"]
    assert len(cat) >= len(seed_catalog.SEED)
    assert cat[0]["name"] == "食パン" and cat[0]["category"] == "bakery"
    assert "bread" in cat[0]["aliases"]


def test_seeded_rows_have_categories_in_state():
    seed_catalog.seed(appmod.conn)
    iid = str(uuid.uuid4())
    client.post("/api/op", json={"op_id": str(uuid.uuid4()), "actor": "t",
                                 "type": "add", "name": "とうふ", "item_id": iid})
    items = {i["name"]: i for i in client.get("/api/state").json()["items"]}
    assert items["豆腐"]["category"] == "pantry"  # alias→seed row, category immediate
