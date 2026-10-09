"""Drop English names that are a DIFFERENT product from catalog rows (PLAN.md 2026-10-09).

The starter catalog and the old LLM merges folded some English names onto a
Japanese item because that is how Japanese uses the word. At a US store they are
other things: "paprika" is the spice, not パプリカ (the red bell pepper), "radish"
is not 大根 (daikon), "leek" is not 長ねぎ, "orange" is not みかん. Typing one of
them landed on the wrong row — "red bell pepper" came out as "paprika".

    python server/repair_aliases.py            # dry run: says what it WOULD do
    python server/repair_aliases.py --apply    # backs the DB up first, then does it

Only the aliases listed in WRONG_ALIASES are removed (and, for パプリカ, "red bell
pepper" is made sure of), in one transaction; nothing else on a row, and no
list item or purchase, is touched. Running it twice does nothing the second time.
The same table drives seed_catalog's tests, so the seed cannot bring them back.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import db

# display name -> aliases that name a different product (compared canonically)
WRONG_ALIASES: dict[str, list[str]] = {
    "パプリカ": ["paprika"],
    "大根": ["radish"],
    "長ねぎ": ["leek"],
    "かぼちゃ": ["pumpkin"],          # kabocha is its own squash
    "みかん": ["orange"],             # a mandarin is not an orange
    "シリアル": ["granola", "グラノーラ"],
}
# display name -> aliases that must be present (the right English name)
ENSURE_ALIASES: dict[str, list[str]] = {"パプリカ": ["red bell pepper"]}


def plan(conn: sqlite3.Connection) -> list[dict]:
    steps = []
    for r in conn.execute("SELECT id, display_name, aliases_json FROM item_catalog ORDER BY id"):
        if r["display_name"] not in WRONG_ALIASES and r["display_name"] not in ENSURE_ALIASES:
            continue
        aliases = json.loads(r["aliases_json"])
        drop = {db.canonical(a) for a in WRONG_ALIASES.get(r["display_name"], [])}
        kept = [a for a in aliases if db.canonical(a) not in drop]
        have = {db.canonical(a) for a in kept}
        for a in ENSURE_ALIASES.get(r["display_name"], []):
            if db.canonical(a) not in have:
                kept.append(a)
        if kept != aliases:
            steps.append({"id": r["id"], "name": r["display_name"], "before": aliases, "after": kept})
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (after a backup)")
    ap.add_argument("--db", type=Path, default=db.DB_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    steps = plan(ro)
    if not steps:
        print("nothing to repair")
        return 0
    for s in steps:
        listed = ro.execute("SELECT COUNT(*) FROM items WHERE catalog_id=?", (s["id"],)).fetchone()[0]
        bought = ro.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (s["id"],)).fetchone()[0]
        print(f"{s['name']!r} (#{s['id']}, on list: {listed}, purchases: {bought}): "
              f"{s['before']} -> {s['after']}")
    ro.close()
    if not args.apply:
        print("\ndry run — nothing changed; re-run with --apply")
        return 0

    # backup through a PLAIN connection, before db.connect() runs migrations
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.db.with_name(f"{args.db.stem}.pre-aliases-{stamp}.db")
    raw = sqlite3.connect(args.db)
    with sqlite3.connect(backup) as dst:
        raw.backup(dst)
    raw.close()
    print(f"\nbackup: {backup}")

    conn = db.connect(args.db)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        steps = plan(conn)          # re-derived inside the transaction
        for s in steps:
            conn.execute("UPDATE item_catalog SET aliases_json=? WHERE id=?",
                         (json.dumps(s["after"], ensure_ascii=False), s["id"]))
        if steps:
            db.bump_revision(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"repaired {len(steps)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
