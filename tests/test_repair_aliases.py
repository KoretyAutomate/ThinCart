"""server/repair_aliases.py and the seed — English names that are other products.

Owner's report (2026-10-09): "red bell pepper" came out as "paprika". The seed
mapped パプリカ to "paprika" (Japanese usage); in English that is the spice. The
same flaw put "radish" on 大根, "leek" on 長ねぎ, "orange" on みかん, "pumpkin" on
かぼちゃ and "granola" on シリアル. Every name here is copied from the live catalog.
"""

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

os.environ["THINCART_DB"] = str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db")
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import catalog
import db
import repair_aliases
import seed_catalog

LIVE_ROWS = [  # as they stand in the live catalog before the repair
    ("パプリカ", ["paprika", "red bell pepper"]),
    ("大根", ["だいこん", "ダイコン", "daikon", "radish"]),
    ("長ねぎ", ["ねぎ", "ネギ", "長ネギ", "green onion", "leek"]),
    ("かぼちゃ", ["カボチャ", "南瓜", "pumpkin", "kabocha"]),
    ("みかん", ["ミカン", "蜜柑", "mandarin", "orange"]),
    ("シリアル", ["グラノーラ", "cereal", "granola"]),
    ("ピーマン", ["bell pepper", "green pepper"]),          # fine: must not be touched
]


def _db(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "t.db")
    for name, aliases in LIVE_ROWS:
        conn.execute("INSERT INTO item_catalog(canonical_name, display_name, aliases_json) VALUES(?,?,?)",
                     (db.canonical(name), name, json.dumps(aliases, ensure_ascii=False)))
    conn.commit()
    return conn


def _aliases(conn, name):
    return json.loads(conn.execute("SELECT aliases_json FROM item_catalog WHERE display_name=?", (name,)).fetchone()[0])


def test_the_seed_no_longer_carries_them():
    for name, _cat, _edible, aliases in seed_catalog.SEED:
        wrong = {db.canonical(a) for a in repair_aliases.WRONG_ALIASES.get(name, [])}
        assert not wrong & {db.canonical(a) for a in aliases}, name
    seeded = {name: aliases for name, _c, _e, aliases in seed_catalog.SEED}
    assert "red bell pepper" in seeded["パプリカ"]


def test_repair_drops_only_the_wrong_names(tmp_path):
    conn = _db(tmp_path)
    steps = repair_aliases.plan(conn)
    assert {s["name"] for s in steps} == set(repair_aliases.WRONG_ALIASES)
    for s in steps:
        conn.execute("UPDATE item_catalog SET aliases_json=? WHERE id=?",
                     (json.dumps(s["after"], ensure_ascii=False), s["id"]))
    assert _aliases(conn, "パプリカ") == ["red bell pepper"]
    assert _aliases(conn, "大根") == ["だいこん", "ダイコン", "daikon"]
    assert _aliases(conn, "シリアル") == ["cereal"]
    assert _aliases(conn, "ピーマン") == ["bell pepper", "green pepper"]
    assert repair_aliases.plan(conn) == []                      # nothing left to do: idempotent


def test_red_bell_pepper_shows_as_itself_and_paprika_is_the_spice(tmp_path):
    conn = _db(tmp_path)
    for s in repair_aliases.plan(conn):
        conn.execute("UPDATE item_catalog SET aliases_json=? WHERE id=?",
                     (json.dumps(s["after"], ensure_ascii=False), s["id"]))
    row = conn.execute("SELECT id, aliases_json FROM item_catalog WHERE display_name='パプリカ'").fetchone()
    assert db.name_en(row["aliases_json"], "パプリカ") == "red bell pepper"       # not "paprika"
    assert db.get_or_create_catalog(conn, "red bell pepper") == row["id"]
    spice = db.get_or_create_catalog(conn, "paprika")
    assert spice != row["id"]                                                     # its own item
    japanese = {r["id"] for r in conn.execute(
        "SELECT id FROM item_catalog WHERE display_name IN ('大根','長ねぎ','みかん','シリアル')")}
    for typed in ("radish", "leek", "orange", "granola"):
        assert db.get_or_create_catalog(conn, typed) not in japanese


def test_the_enrichment_guard_still_keeps_the_spice_apart():
    """A later LLM 'alias_of' answer must not fold paprika back onto パプリカ."""
    assert catalog._english_mismatch("paprika", "paprika", ["パプリカ", "red bell pepper"]) is True
    assert catalog._english_mismatch("radish", "radish", ["大根", "だいこん", "daikon"]) is True


def test_apply_makes_a_backup_and_changes_the_database(tmp_path, monkeypatch, capsys):
    conn = _db(tmp_path)
    conn.close()
    monkeypatch.setattr(sys, "argv", ["repair_aliases.py", "--apply", "--db", str(tmp_path / "t.db")])
    assert repair_aliases.main() == 0
    assert list(tmp_path.glob("t.pre-aliases-*.db")), "no backup taken"
    check = sqlite3.connect(tmp_path / "t.db")
    got = check.execute("SELECT aliases_json FROM item_catalog WHERE display_name='パプリカ'").fetchone()[0]
    assert json.loads(got) == ["red bell pepper"]
    monkeypatch.setattr(sys, "argv", ["repair_aliases.py", "--apply", "--db", str(tmp_path / "t.db")])
    assert repair_aliases.main() == 0
    assert "nothing to repair" in capsys.readouterr().out         # the second run does nothing
