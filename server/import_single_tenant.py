"""
import_single_tenant.py — one-off import of a MASTER (single-tenant) ThinCart
database into the multi-tenant SaaS DB, under one freshly-created household.

The master schema has no household_id and keeps snooze on item_catalog; this maps
its global list + history into a single household owned by the given user.

Usage:
    python import_single_tenant.py --source /path/to/master/thincart.db \\
        --email you@example.com --password 'yourpass' --name Korehito \\
        [--household "Home"] [--db /path/to/saas/thincart.db]

Idempotency: catalog rows merge by canonical_name; re-running would duplicate the
household + list, so run once into a fresh SaaS DB (or a throwaway to preview).
"""
import argparse
import json
import sqlite3
import uuid

import auth
import config
import db


def _catalog_map(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[int, int]:
    """Copy master catalog rows into the shared SaaS catalog, return old->new id map.

    Tolerant of master schema drift (older DBs may lack `verified`)."""
    cols = {r["name"] for r in src.execute("PRAGMA table_info(item_catalog)")}
    has_verified = "verified" in cols
    mapping: dict[int, int] = {}
    for r in src.execute("SELECT * FROM item_catalog"):
        existing = dst.execute(
            "SELECT id FROM item_catalog WHERE canonical_name=?", (r["canonical_name"],)
        ).fetchone()
        if existing:
            mapping[r["id"]] = existing["id"]
            continue
        cur = dst.execute(
            "INSERT INTO item_catalog(canonical_name, display_name, aliases_json, category, "
            "plants_json, is_edible, verified, llm_enriched_at) VALUES(?,?,?,?,?,?,?,?)",
            (r["canonical_name"], r["display_name"], r["aliases_json"] or "[]", r["category"],
             r["plants_json"], r["is_edible"],
             (r["verified"] if has_verified else 1), r["llm_enriched_at"]),
        )
        mapping[r["id"]] = cur.lastrowid
    return mapping


def run(source: str, email: str, password: str, name: str, household_name: str,
        dst_path: str | None = None) -> dict:
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    dst = db.connect(dst_path or config.DB_PATH)

    uid = auth.create_user(dst, email, password, name)
    hid = auth.create_household(dst, household_name or f"{name}'s list", uid)
    cmap = _catalog_map(src, dst)

    n_items = n_events = 0
    for r in src.execute("SELECT * FROM items"):
        dst.execute(
            "INSERT INTO items(id, household_id, catalog_id, qty_note, added_by, added_at, revision) "
            "VALUES(?,?,?,?,?,?,?)",
            (r["id"], hid, cmap[r["catalog_id"]], r["qty_note"], r["added_by"], r["added_at"], 0),
        )
        n_items += 1
    for r in src.execute("SELECT * FROM purchase_events"):
        dst.execute(
            "INSERT INTO purchase_events(household_id, catalog_id, bought_at, bought_by, source) "
            "VALUES(?,?,?,?,?)",
            (hid, cmap[r["catalog_id"]], r["bought_at"], r["bought_by"], "checkoff"),
        )
        n_events += 1
    # carry over master's per-catalog snooze (it lived on item_catalog there)
    try:
        for r in src.execute(
            "SELECT id, snoozed_until FROM item_catalog WHERE snoozed_until IS NOT NULL"
        ):
            db.set_snooze(dst, hid, cmap[r["id"]], r["snoozed_until"])
    except sqlite3.OperationalError:
        pass  # master без snooze column — fine

    dst.execute("UPDATE households SET revision = ? WHERE id = ?", (n_items + n_events, hid))
    dst.commit()
    return {"household_id": hid, "user_id": uid, "items": n_items,
            "events": n_events, "catalog_rows": len(cmap),
            "invite_code": auth.household_summary(dst, hid)["invite_code"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--name", default="Me")
    p.add_argument("--household", default="")
    p.add_argument("--db", default=None)
    a = p.parse_args()
    result = run(a.source, a.email, a.password, a.name, a.household, a.db)
    print(json.dumps(result, indent=2, ensure_ascii=False))
