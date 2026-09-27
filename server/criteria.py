"""
criteria.py — the catalog-level half of an edit (split out of app.py).

Category, purchase criteria (note, budget, preferred store) and the standing
preferences of PLAN.md Phase 7 (organic, brand). They live on the catalog row,
not the list entry, so they survive checkoff → re-add.
"""

import sqlite3
import unicodedata

from fastapi import HTTPException

import catalog
import db
from ops import Op


def parse_budget(raw: str) -> float | None:
    """Lenient price parse — JP keyboards produce full-width digits and ¥/円.
    None = unparseable (field is IGNORED, the rest of the edit still applies);
    a 422 here would silently drop the whole op client-side."""
    t = unicodedata.normalize("NFKC", raw)
    t = t.replace("¥", "").replace("円", "").replace(",", "").strip()
    try:
        v = float(t)
        return v if v >= 0 else None
    except ValueError:
        return None


def apply(conn: sqlite3.Connection, op: Op, catalog_id: int, rename_organic: bool) -> bool:
    """The catalog-level half of an edit: category, purchase criteria, and the
    standing preferences. They live on the catalog row, so they survive
    checkoff → re-add. Returns whether anything was written."""
    changed = False
    if op.category is not None:
        if op.category not in catalog.CATEGORIES:
            raise HTTPException(422, "invalid category")
        conn.execute("UPDATE item_catalog SET category=? WHERE id=?", (op.category, catalog_id))
        changed = True
    if op.note is not None:
        conn.execute("UPDATE item_catalog SET note=? WHERE id=?", (op.note.strip(), catalog_id))
        changed = True
    if op.budget is not None:
        if not op.budget.strip():  # "" clears
            conn.execute("UPDATE item_catalog SET budget=NULL WHERE id=?", (catalog_id,))
            changed = True
        else:
            val = parse_budget(op.budget)
            if val is not None:
                conn.execute("UPDATE item_catalog SET budget=? WHERE id=?", (val, catalog_id))
                changed = True
    if op.store is not None:
        sid = db.get_or_create_store(conn, op.store)  # None when "" → clears
        conn.execute("UPDATE item_catalog SET preferred_store_id=? WHERE id=?", (sid, catalog_id))
        changed = True
    # An explicit toggle wins over a qualifier typed in the same save; a plain
    # rename never CLEARS organic — only the toggle does.
    organic = op.organic if op.organic is not None else (True if rename_organic else None)
    if organic is not None:
        conn.execute("UPDATE item_catalog SET organic=? WHERE id=?", (int(organic), catalog_id))
        changed = True
    if op.brand is not None:
        conn.execute("UPDATE item_catalog SET brand=? WHERE id=?", (op.brand.strip(), catalog_id))
        changed = True
    return changed
