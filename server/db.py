"""
db.py — PlantCart SQLite layer (WAL). One DB file, server is the source of truth.

Concurrency model: FastAPI is async but ops are tiny; a single connection guarded
by an asyncio.Lock in app.py serializes all writes. SQLite WAL keeps readers cheap.
"""
import json
import os
import sqlite3
import unicodedata
from pathlib import Path

DB_PATH = Path(os.environ.get("PLANTCART_DB", Path(__file__).parent / "data" / "plantcart.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_catalog(
  id INTEGER PRIMARY KEY,
  canonical_name TEXT UNIQUE NOT NULL,   -- NFKC-folded, lowered, trimmed
  display_name TEXT NOT NULL,            -- as the user first typed it
  aliases_json TEXT NOT NULL DEFAULT '[]',
  category TEXT,                         -- produce / dairy / pantry … (LLM, Phase 2)
  plants_json TEXT,                      -- distinct edible plants (LLM, Phase 2)
  is_edible INTEGER,
  snoozed_until TEXT,                    -- server-side snooze: syncs to both phones
  llm_enriched_at TEXT
);

CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,        -- client-generated UUID: offline add→checkoff works
  catalog_id INTEGER NOT NULL REFERENCES item_catalog(id),
  qty_note TEXT NOT NULL DEFAULT '',
  added_by TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_events(
  id INTEGER PRIMARY KEY,
  catalog_id INTEGER NOT NULL REFERENCES item_catalog(id),
  bought_at TEXT NOT NULL,
  bought_by TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'checkoff' CHECK(source IN ('checkoff'))
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS applied_ops(
  op_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}'  -- what undo needs (event_id, item snapshot)
);

CREATE INDEX IF NOT EXISTS idx_events_catalog ON purchase_events(catalog_id, bought_at);
CREATE INDEX IF NOT EXISTS idx_items_catalog ON items(catalog_id);
"""


def canonical(name: str) -> str:
    """NFKC fold (full-width→half-width, ﾐﾙｸ→ミルク), lower, collapse whitespace.

    This is the no-LLM canonicalization path; the LLM alias-merge (Phase 1) maps
    *different spellings* (たまねぎ vs 玉ねぎ) onto one catalog row on top of this.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(folded.split())


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('revision', '0')")
    conn.commit()
    return conn


def bump_revision(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='revision' "
        "RETURNING CAST(value AS INTEGER)"
    )
    return cur.fetchone()[0]


def get_revision(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])


def get_or_create_catalog(conn: sqlite3.Connection, name: str) -> int:
    canon = canonical(name)
    row = conn.execute(
        "SELECT id FROM item_catalog WHERE canonical_name=?", (canon,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO item_catalog(canonical_name, display_name) VALUES(?, ?)",
        (canon, name.strip()),
    )
    return cur.lastrowid


def purchase_history(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """catalog_id -> ordered purchase timestamps (the cycle-estimator substrate)."""
    hist: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT catalog_id, bought_at FROM purchase_events ORDER BY bought_at"
    ):
        hist.setdefault(r["catalog_id"], []).append(r["bought_at"])
    return hist


def suggestions(conn: sqlite3.Connection, now) -> list[dict]:
    """Due items (cycles.suggest) minus already-listed and snoozed catalog rows."""
    import cycles

    on_list = {r["catalog_id"] for r in conn.execute("SELECT catalog_id FROM items")}
    now_iso = now.isoformat(timespec="seconds")
    out = []
    for s in cycles.suggest(purchase_history(conn), now):
        if s["catalog_id"] in on_list:
            continue
        row = conn.execute(
            "SELECT display_name, snoozed_until FROM item_catalog WHERE id=?",
            (s["catalog_id"],),
        ).fetchone()
        if row["snoozed_until"] and row["snoozed_until"] > now_iso:
            continue
        out.append({**s, "name": row["display_name"]})
    return out


def state(conn: sqlite3.Connection, now=None) -> dict:
    """Full list state — small enough (tens of items) to always send whole."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    items = [
        dict(r)
        for r in conn.execute(
            """SELECT i.id, c.display_name AS name, c.category, i.qty_note,
                      i.added_by, i.added_at
               FROM items i JOIN item_catalog c ON c.id = i.catalog_id
               ORDER BY COALESCE(c.category, 'zzz'), i.added_at"""
        )
    ]
    import catalog

    week = catalog.weekly_plants(conn, now)
    return {
        "revision": get_revision(conn),
        "items": items,
        "suggestions": suggestions(conn, now),
        "plants": {"count": len(week), "target": 30, "week": week},
    }


def prune_applied_ops(conn: sqlite3.Connection, cutoff_iso: str) -> None:
    conn.execute("DELETE FROM applied_ops WHERE applied_at < ?", (cutoff_iso,))


def record_op(conn: sqlite3.Connection, op_id: str, applied_at: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO applied_ops(op_id, applied_at, result_json) VALUES(?,?,?)",
        (op_id, applied_at, json.dumps(result, ensure_ascii=False)),
    )


def get_applied(conn: sqlite3.Connection, op_id: str) -> dict | None:
    row = conn.execute(
        "SELECT result_json FROM applied_ops WHERE op_id=?", (op_id,)
    ).fetchone()
    return json.loads(row["result_json"]) if row else None
