"""server/merge_organic.py — folding pre-Phase-7 "Organic X" rows into X.

Runs the real script's main() against a throwaway DB, because the thing that
matters is what `--apply` does to a database, and what a dry run does NOT.
"""

import contextlib
import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import db

SCRIPT = Path(__file__).parent.parent / "server" / "merge_organic.py"


def _seed(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    for name, aliases in [
        ("walnuts", "[]"), ("organic walnuts", "[]"),
        ("卵", '["egg"]'), ("Organic Egg", "[]"),
        ("Organic Kale", "[]"),                                  # no base row yet
        ("simple mills organic seed flour crackers", "[]"),     # a product name
    ]:
        conn.execute(
            "INSERT INTO item_catalog(canonical_name, display_name, aliases_json) VALUES(?,?,?)",
            (db.canonical(name), name, aliases),
        )
    ids = {r["display_name"]: r["id"] for r in conn.execute("SELECT id, display_name FROM item_catalog")}
    for name in ("walnuts", "organic walnuts", "Organic Egg"):
        conn.execute("INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?, '2026-09-01T00:00:00+00:00')",
                     (ids[name],))
    # both walnuts rows on the list: must end as ONE entry
    conn.execute("INSERT INTO items(id, catalog_id, added_at, revision) VALUES('a', ?, '2026-09-01', 1)",
                 (ids["walnuts"],))
    conn.execute("INSERT INTO items(id, catalog_id, added_at, revision) VALUES('b', ?, '2026-09-01', 1)",
                 (ids["organic walnuts"],))
    # a pick on each walnuts row at the same chain: the base's wins
    for name, sku in (("walnuts", "BASE"), ("organic walnuts", "ORG")):
        conn.execute(
            "INSERT INTO product_picks(catalog_id, chain, sku, name, brand, pack_size, picked_at) "
            "VALUES(?, 'wegmans', ?, 'w', '', '', '2026-09-01')",
            (ids[name], sku),
        )
    conn.execute("UPDATE item_catalog SET note='raw, unsalted', brand='Diamond' WHERE id=?",
                 (ids["organic walnuts"],))
    conn.commit()
    return conn


_spec = importlib.util.spec_from_file_location("merge_organic", SCRIPT)
assert _spec and _spec.loader
merge_organic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_organic)


def _run(path: Path, *args: str) -> str:
    argv, buf = sys.argv, io.StringIO()
    sys.argv = ["merge_organic.py", "--db", str(path), *args]
    try:
        with contextlib.redirect_stdout(buf):
            assert merge_organic.main() == 0
    finally:
        sys.argv = argv
    return buf.getvalue()


SNAPSHOT = (
    "SELECT * FROM item_catalog ORDER BY id",
    "SELECT * FROM items ORDER BY id",
    "SELECT * FROM purchase_events ORDER BY id",
    "SELECT * FROM product_picks ORDER BY catalog_id, chain",
)


def _snapshot(conn):
    return [tuple(r) for q in SNAPSHOT for r in conn.execute(q)]


def test_dry_run_changes_nothing(tmp_path):
    conn = _seed(tmp_path / "t.db")
    before = _snapshot(conn)
    out = _run(tmp_path / "t.db")
    assert "dry run" in out and "'organic walnuts'" in out
    assert _snapshot(db.connect(tmp_path / "t.db")) == before


def test_apply_folds_history_list_and_picks_onto_the_base(tmp_path):
    _seed(tmp_path / "t.db")
    out = _run(tmp_path / "t.db", "--apply")
    assert "backup:" in out and list(tmp_path.glob("t.pre-organic-*.db"))
    conn = db.connect(tmp_path / "t.db")
    names = {r["display_name"]: r for r in conn.execute("SELECT * FROM item_catalog")}
    assert "organic walnuts" not in names and "Organic Egg" not in names
    walnuts, egg = names["walnuts"], names["卵"]
    assert walnuts["note"] == "raw, unsalted" and walnuts["brand"] == "Diamond"   # carried over
    n = conn.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (walnuts["id"],)).fetchone()[0]
    assert n == 2                                                                 # both histories
    assert conn.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (egg["id"],)).fetchone()[0] == 1
    assert [r["id"] for r in conn.execute("SELECT id FROM items")] == ["a"]      # one list entry
    assert [r["sku"] for r in conn.execute("SELECT sku FROM product_picks")] == ["BASE"]
    # no base row existed: renamed in place
    assert "Kale" in names and "Organic Kale" not in names
    # a product whose NAME contains organic is not touched
    assert "simple mills organic seed flour crackers" in names


def test_apply_twice_is_a_no_op(tmp_path):
    _seed(tmp_path / "t.db")
    _run(tmp_path / "t.db", "--apply")
    before = _snapshot(db.connect(tmp_path / "t.db"))
    assert "nothing to fold" in _run(tmp_path / "t.db", "--apply")
    assert _snapshot(db.connect(tmp_path / "t.db")) == before


def test_dry_run_reports_list_and_pick_exposure(tmp_path):
    _seed(tmp_path / "t.db")
    out = _run(tmp_path / "t.db")
    assert "'organic walnuts' (#2, 1 purchases, on list: yes, picks: 1)" in out


def test_two_variants_with_no_plain_row_become_one_item(tmp_path):
    """Codex review 2026-09-27: both used to be renamed to the base, and the
    second rename hit the UNIQUE name and rolled the whole fold back."""
    conn = db.connect(tmp_path / "k.db")
    for name in ("Organic Kale", "オーガニックKale"):
        conn.execute("INSERT INTO item_catalog(canonical_name, display_name) VALUES(?,?)",
                     (db.canonical(name), name))
    ids = [r["id"] for r in conn.execute("SELECT id FROM item_catalog ORDER BY id")]
    for cid in ids:
        conn.execute("INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?, '2026-09-01T00:00:00+00:00')",
                     (cid,))
    conn.commit()
    _run(tmp_path / "k.db", "--apply")
    conn = db.connect(tmp_path / "k.db")
    rows = conn.execute("SELECT id, display_name FROM item_catalog").fetchall()
    assert [r["display_name"] for r in rows] == ["Kale"]
    assert conn.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (rows[0]["id"],)).fetchone()[0] == 2
