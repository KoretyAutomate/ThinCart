"""
price_reco.py — the last price answer per item, and when it still applies.

PLAN.md Phase 8.1 + review deltas 1–3. Prices are asked only when the owner
opens the plan or refreshes it; the answer is kept here so both phones, and the
list's 🏬 chips, can show it — offline too.

An answer is tied to the QUESTION it answered: `input_key` is a hash of
everything that decides it (the item's English term, brand, wanted amount,
whether organic applies, every chain's picked product, and the priced
stores). `state()` recomputes the key and shows a stored answer only when it
still matches, so an edit, a pick, an enrichment that names the item in
English, or a newly linked store hides it until asked again — nothing has to
remember to invalidate it.
"""

import hashlib
import json
import sqlite3


def organic_applies(conn: sqlite3.Connection, catalog_id: int) -> bool:
    """The household organic setting is for FOOD (PLAN.md 8.0): paper towels
    searched as "organic paper towels" find nothing anywhere."""
    import db

    if not db.organic_setting(conn):
        return False
    row = conn.execute("SELECT is_edible FROM item_catalog WHERE id=?", (catalog_id,)).fetchone()
    return bool(row and row[0] == 1)


def priced_stores(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, chain, chain_store_id FROM stores WHERE chain != '' AND chain_store_id != '' ORDER BY id"
    )]


def input_key(conn: sqlite3.Connection, catalog_id: int, stores: list[dict] | None = None) -> str | None:
    """The question an answer for this item must have been asked as."""
    import db

    r = conn.execute(
        "SELECT display_name, aliases_json, brand, buy_qty FROM item_catalog WHERE id=?", (catalog_id,)
    ).fetchone()
    if r is None:
        return None
    term = db.name_en(r["aliases_json"], r["display_name"]) or r["display_name"]
    picks = sorted(
        (p["chain"], p["sku"])
        for p in conn.execute("SELECT chain, sku FROM product_picks WHERE catalog_id=?", (catalog_id,))
    )
    stores = priced_stores(conn) if stores is None else stores
    parts = [term, (r["brand"] or "").strip().lower(), (r["buy_qty"] or "").strip().lower(),
             organic_applies(conn, catalog_id), picks, [(s["id"], s["chain_store_id"]) for s in stores]]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def save(conn: sqlite3.Connection, catalog_id: int, key: str, answer: dict, ts: str) -> bool:
    """Store a cheapest answer — only if the item's question is still `key`
    (a slow answer must not overwrite a newer question). Caller holds the
    write lock. True when written."""
    if input_key(conn, catalog_id) != key or not answer.get("cheapest"):
        return False
    conn.execute(
        """INSERT INTO price_reco(catalog_id, store_id, input_key, answer_json, computed_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(catalog_id) DO UPDATE SET store_id=excluded.store_id,
             input_key=excluded.input_key, answer_json=excluded.answer_json,
             computed_at=excluded.computed_at""",
        (catalog_id, answer["cheapest"]["store_id"], key, json.dumps(answer, ensure_ascii=False), ts),
    )
    return True


def stored_for(conn: sqlite3.Connection, catalog_id: int, key: str) -> bool:
    """An answer to this very question is already stored."""
    row = conn.execute("SELECT input_key FROM price_reco WHERE catalog_id=?", (catalog_id,)).fetchone()
    return bool(row and row["input_key"] == key)


def forget(conn: sqlite3.Connection, catalog_id: int, key: str) -> None:
    """Every store answered the current question and none gives a cheapest
    store. A failure to ask is NOT this — the last answer is kept, with its age."""
    if input_key(conn, catalog_id) == key:
        conn.execute("DELETE FROM price_reco WHERE catalog_id=?", (catalog_id,))


def current(conn: sqlite3.Connection) -> dict[int, dict]:
    """catalog_id -> stored answer, for every item whose question is unchanged."""
    stores = priced_stores(conn)
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT catalog_id, input_key, answer_json, computed_at FROM price_reco"):
        if input_key(conn, r["catalog_id"], stores) == r["input_key"]:
            answer = json.loads(r["answer_json"])
            answer["computed_at"] = r["computed_at"]
            out[r["catalog_id"]] = answer
    return out
