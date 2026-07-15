"""Per-item emoji icons: curated lookup, LLM-emoji validation, DB wiring."""
import os
import sys
import uuid
from pathlib import Path

os.environ["THINCART_DB"] = str(
    Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import db  # noqa: E402
import emoji  # noqa: E402


def test_curated_lookup_hits():
    assert emoji.lookup("avocado") == "🥑"
    assert emoji.lookup("banana") == "🍌"
    assert emoji.lookup("apple") == "🍎"


def test_lookup_folds_case_width_and_japanese():
    assert emoji.lookup("AVOCADO") == "🥑"          # case-insensitive
    assert emoji.lookup("  Banana ") == "🍌"         # whitespace collapse
    assert emoji.lookup("たまねぎ") == "🧅"           # JA
    assert emoji.lookup("玉ねぎ") == "🧅"             # JA alt spelling
    assert emoji.lookup("ﾊﾞﾅﾅ") == "🍌"             # half-width kana → NFKC


def test_lookup_miss_returns_none():
    assert emoji.lookup("one mighty mill bagel") is None   # brand: not an exact key
    assert emoji.lookup("qwertyzxcv") is None


def test_is_emoji_validation():
    assert emoji.is_emoji("🥑")
    assert emoji.is_emoji("🌶️")          # with variation selector
    assert not emoji.is_emoji("avocado")  # plain text
    assert not emoji.is_emoji("")
    assert not emoji.is_emoji(None)
    assert not emoji.is_emoji("🥑 avocado")   # emoji + letters
    assert not emoji.is_emoji("x" * 20)


def test_new_catalog_row_gets_curated_emoji():
    conn = db.connect()
    cid = db.get_or_create_catalog(conn, "Avocado")
    row = conn.execute("SELECT emoji FROM item_catalog WHERE id=?", (cid,)).fetchone()
    assert row["emoji"] == "🥑"
    # an item with no curated match stores NULL (LLM/category fallback fills later)
    cid2 = db.get_or_create_catalog(conn, "Mighty Mill bagel")
    row2 = conn.execute("SELECT emoji FROM item_catalog WHERE id=?", (cid2,)).fetchone()
    assert row2["emoji"] is None
    conn.commit()   # release the WAL write lock for the next test's connection
    conn.close()


def test_state_includes_emoji_field():
    conn = db.connect()
    cid = db.get_or_create_catalog(conn, "Banana")
    conn.execute(
        "INSERT INTO items(id, catalog_id, added_at, revision) VALUES(?,?,?,?)",
        (f"itm-{uuid.uuid4().hex}", cid, "2026-07-15T00:00:00+00:00", db.bump_revision(conn)),
    )
    conn.commit()
    st = db.state(conn)
    banana = next(i for i in st["items"] if i["name"] == "Banana")
    assert banana["emoji"] == "🍌"
    conn.close()
