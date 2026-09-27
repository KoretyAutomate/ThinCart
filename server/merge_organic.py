"""Fold "Organic X" catalog rows into X (PLAN.md Phase 7A).

Before Phase 7 "organic onion" became a second item beside Onion, with its own
purchase history. This moves each such row onto its base item — list entry,
purchases and product picks — and deletes the duplicate. Whether the household
buys organic is a household setting, not something this records per item.

    python server/merge_organic.py            # dry run: says what it WOULD do
    python server/merge_organic.py --apply    # backs the DB up first, then does it

Only a LEADING qualifier counts (db.split_organic); a brand's product name with
organic mid-name is left alone. Running it twice does nothing the second time.

The dry run opens the database read-only. --apply takes an SQLite online backup
(safe with the service running, WAL and all) and then folds everything in one
BEGIN IMMEDIATE transaction. A deleted row's id is not redirected: an op still
queued on a phone that names it would find nothing — which is why the report
lists whether each row is on the list or has a product pick before you apply.
"""

import argparse
import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

import db


def find_base(conn: sqlite3.Connection, base: str, not_id: int) -> int | None:
    """The row the base name resolves to — exact, then alias — never creating one."""
    canon = db.canonical(base)
    row = conn.execute("SELECT id FROM item_catalog WHERE canonical_name=? AND id != ?", (canon, not_id)).fetchone()
    if row:
        return row["id"]
    rows = conn.execute("SELECT id, aliases_json FROM item_catalog WHERE aliases_json != '[]' AND id != ?", (not_id,))
    for r in rows:
        if any(db.canonical(a) == canon for a in json.loads(r["aliases_json"])):
            return r["id"]
    return None


def plan(conn: sqlite3.Connection) -> list[dict]:
    steps = []
    for r in conn.execute("SELECT id, canonical_name, display_name FROM item_catalog ORDER BY id"):
        base, organic = db.split_organic(r["display_name"])
        if not organic:
            continue
        target = find_base(conn, base, r["id"])
        steps.append({"id": r["id"], "name": r["display_name"], "base": base, "target": target})
    return steps


def merge_into(conn: sqlite3.Connection, src: int, dst: int) -> None:
    """Move everything that belongs to row `src` onto row `dst`, then drop `src`."""
    listed = conn.execute("SELECT id FROM items WHERE catalog_id=?", (dst,)).fetchone()
    if listed:  # the base is already on the list: one entry, not two
        conn.execute("DELETE FROM items WHERE catalog_id=?", (src,))
    else:
        conn.execute("UPDATE items SET catalog_id=? WHERE catalog_id=?", (dst, src))
    conn.execute("UPDATE purchase_events SET catalog_id=? WHERE catalog_id=?", (dst, src))
    # one pick per (item, chain): the base's own pick wins where both have one
    conn.execute(
        "DELETE FROM product_picks WHERE catalog_id=? AND chain IN "
        "(SELECT chain FROM product_picks WHERE catalog_id=?)",
        (src, dst),
    )
    conn.execute("UPDATE product_picks SET catalog_id=? WHERE catalog_id=?", (dst, src))
    s = conn.execute(
        "SELECT note, budget, preferred_store_id, brand, snoozed_until FROM item_catalog WHERE id=?", (src,)
    ).fetchone()
    conn.execute(
        "UPDATE item_catalog SET "
        "note = CASE WHEN note='' THEN ? ELSE note END, "
        "budget = COALESCE(budget, ?), "
        "preferred_store_id = COALESCE(preferred_store_id, ?), "
        "brand = CASE WHEN brand='' THEN ? ELSE brand END WHERE id=?",
        (s["note"], s["budget"], s["preferred_store_id"], s["brand"], dst),
    )
    conn.execute("DELETE FROM item_catalog WHERE id=?", (src,))


def rename_in_place(conn: sqlite3.Connection, src: int, base: str) -> None:
    conn.execute(
        "UPDATE item_catalog SET canonical_name=?, display_name=? WHERE id=?",
        (db.canonical(base), base, src),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (after a backup)")
    ap.add_argument("--db", type=Path, default=db.DB_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db}")
        return 1
    # read-only for the dry run: no schema migration, nothing committed
    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    steps = plan(ro)
    if not steps:
        print("nothing to fold")
        return 0
    for s in steps:
        n = ro.execute("SELECT COUNT(*) FROM purchase_events WHERE catalog_id=?", (s["id"],)).fetchone()[0]
        listed = ro.execute("SELECT COUNT(*) FROM items WHERE catalog_id=?", (s["id"],)).fetchone()[0]
        picks = ro.execute("SELECT COUNT(*) FROM product_picks WHERE catalog_id=?", (s["id"],)).fetchone()[0]
        where = f"#{s['id']}, {n} purchases, on list: {'yes' if listed else 'no'}, picks: {picks}"
        if s["target"]:
            t = ro.execute("SELECT display_name FROM item_catalog WHERE id=?", (s["target"],)).fetchone()[0]
            print(f"{s['name']!r} ({where}) -> {t!r} (#{s['target']})")
        else:
            print(f"{s['name']!r} ({where}) -> renamed {s['base']!r}")
    ro.close()
    if not args.apply:
        print("\ndry run — nothing changed; re-run with --apply")
        return 0

    conn = db.connect(args.db)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.db.with_name(f"{args.db.stem}.pre-organic-{stamp}.db")
    with sqlite3.connect(backup) as dst:
        conn.backup(dst)
    print(f"\nbackup: {backup}")
    # One write transaction, taken up front: the plan is re-derived inside it so
    # nothing the service wrote since the dry run is folded on stale ids.
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        steps = plan(conn)
        for s in steps:
            # Resolved again at each step, not from the plan: two variants of an
            # item with no plain row ("Organic Kale", "オーガニックKale") — the
            # first is renamed to Kale, and the second must then fold INTO it
            # rather than try to become a second Kale.
            target = find_base(conn, s["base"], s["id"])
            if target:
                merge_into(conn, s["id"], target)
            else:
                rename_in_place(conn, s["id"], s["base"])
        db.bump_revision(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"folded {len(steps)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
