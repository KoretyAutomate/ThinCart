"""
db.py — PlantCart multi-tenant SQLite layer (WAL).

Tenancy model:
- item_catalog is GLOBAL (a shared product corpus — the 173-item seed + LLM
  plant/category enrichment is identical for everyone, expensive to rebuild).
- items, purchase_events, catalog_snooze are PER-HOUSEHOLD (household_id scoped).
- revision is per-household (households.revision); each household's WS stream has
  its own monotonic counter.

Every list/history/plant/suggestion query REQUIRES a household_id — there is no
unscoped read path, so one household can never see another's data.
"""
import json
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import config

DB_PATH = config.DB_PATH

SCHEMA = """
-- ---- accounts / tenancy ----
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  display_name TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS households(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, invite_code TEXT UNIQUE NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS household_members(
  household_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
  joined_at TEXT NOT NULL, PRIMARY KEY(household_id, user_id));

-- ---- shared global catalog ----
CREATE TABLE IF NOT EXISTS item_catalog(
  id INTEGER PRIMARY KEY,
  canonical_name TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  category TEXT, plants_json TEXT, is_edible INTEGER,
  verified INTEGER NOT NULL DEFAULT 1,
  llm_enriched_at TEXT);

-- ---- per-household state ----
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  household_id TEXT NOT NULL,
  catalog_id INTEGER NOT NULL REFERENCES item_catalog(id),
  qty_note TEXT NOT NULL DEFAULT '',
  added_by TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  revision INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS purchase_events(
  id INTEGER PRIMARY KEY,
  household_id TEXT NOT NULL,
  catalog_id INTEGER NOT NULL REFERENCES item_catalog(id),
  bought_at TEXT NOT NULL, bought_by TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'checkoff' CHECK(source IN ('checkoff')));

CREATE TABLE IF NOT EXISTS catalog_snooze(
  household_id TEXT NOT NULL, catalog_id INTEGER NOT NULL,
  snoozed_until TEXT NOT NULL, PRIMARY KEY(household_id, catalog_id));

-- Per-household category preference (long-press editor). The shared
-- item_catalog.category stays the corpus default; a household's edit must not
-- re-categorize the item for everyone. No FK on purpose: alias-merge repoints
-- these rows explicitly (a FK would make the merge DELETE raise instead).
CREATE TABLE IF NOT EXISTS catalog_category_override(
  household_id TEXT NOT NULL, catalog_id INTEGER NOT NULL,
  category TEXT NOT NULL, PRIMARY KEY(household_id, catalog_id));

CREATE TABLE IF NOT EXISTS applied_ops(
  op_id TEXT PRIMARY KEY, household_id TEXT NOT NULL,
  applied_at TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}');

CREATE INDEX IF NOT EXISTS idx_events_hh ON purchase_events(household_id, catalog_id, bought_at);
CREATE INDEX IF NOT EXISTS idx_items_hh ON items(household_id, catalog_id);
CREATE INDEX IF NOT EXISTS idx_members_user ON household_members(user_id);
"""


def canonical(name: str) -> str:
    """NFKC fold (full-width→half-width), casefold, collapse whitespace."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def connect(path: Path = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---- per-household revision ----

def bump_revision(conn: sqlite3.Connection, hid: str) -> int:
    return conn.execute(
        "UPDATE households SET revision = revision + 1 WHERE id=? RETURNING revision", (hid,)
    ).fetchone()[0]


def get_revision(conn: sqlite3.Connection, hid: str) -> int:
    row = conn.execute("SELECT revision FROM households WHERE id=?", (hid,)).fetchone()
    return int(row["revision"]) if row else 0


# ---- global catalog (shared corpus) ----

def get_or_create_catalog(conn: sqlite3.Connection, name: str) -> int:
    canon = canonical(name)
    row = conn.execute("SELECT id FROM item_catalog WHERE canonical_name=?", (canon,)).fetchone()
    if row:
        return row["id"]
    for r in conn.execute("SELECT id, aliases_json FROM item_catalog WHERE aliases_json != '[]'"):
        if any(canonical(a) == canon for a in json.loads(r["aliases_json"])):
            return r["id"]
    return conn.execute(
        "INSERT INTO item_catalog(canonical_name, display_name) VALUES(?, ?)",
        (canon, name.strip()),
    ).lastrowid


def name_en(aliases_json: str, display: str) -> str | None:
    """English display name.

    If the user typed English/ASCII ('White rice', 'One Mighty Mill bagel'),
    ALWAYS show exactly that — a banked generic alias ('rice', 'bagel') must
    never shadow the specific name the user chose. Only for a non-ASCII
    (Japanese) display do we fall back to the first ASCII alias, else None."""
    if display.isascii() and display.strip():
        return display
    for a in json.loads(aliases_json or "[]"):
        if a.isascii() and a.strip():
            return a
    return None


# ---- per-household reads (all REQUIRE hid) ----

def purchase_history(conn: sqlite3.Connection, hid: str) -> dict[int, list[str]]:
    hist: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT catalog_id, bought_at FROM purchase_events WHERE household_id=? ORDER BY bought_at",
        (hid,),
    ):
        hist.setdefault(r["catalog_id"], []).append(r["bought_at"])
    return hist


def _snoozed_map(conn, hid: str, now_iso: str) -> set[int]:
    return {
        r["catalog_id"]
        for r in conn.execute(
            "SELECT catalog_id FROM catalog_snooze WHERE household_id=? AND snoozed_until > ?",
            (hid, now_iso),
        )
    }


def recent_history(conn: sqlite3.Connection, hid: str, limit: int = 100) -> list[dict]:
    """Recent purchase events for ONE household, newest first, joined to catalog.

    The substrate for the History panel: a mis-swipe logs a spurious
    purchase_event that the ~8 s undo toast can no longer reach once it's gone,
    so the panel exposes each event (by its server id) for after-the-fact repair.
    """
    out = []
    for r in conn.execute(
        """SELECT e.id AS event_id, e.catalog_id, e.bought_at, e.bought_by,
                  c.display_name AS name, c.aliases_json
           FROM purchase_events e JOIN item_catalog c ON c.id = e.catalog_id
           WHERE e.household_id=?
           ORDER BY e.bought_at DESC, e.id DESC
           LIMIT ?""",
        (hid, limit),
    ):
        out.append({
            "event_id": r["event_id"],
            "catalog_id": r["catalog_id"],
            "name": r["name"],
            "name_en": name_en(r["aliases_json"], r["name"]),
            "bought_at": r["bought_at"],
            "bought_by": r["bought_by"],
        })
    return out


def suggestions(conn: sqlite3.Connection, hid: str, now) -> list[dict]:
    """Due items (cycles.suggest) minus already-listed and snoozed catalog rows."""
    import cycles

    on_list = {r["catalog_id"] for r in conn.execute(
        "SELECT catalog_id FROM items WHERE household_id=?", (hid,))}
    now_iso = now.isoformat(timespec="seconds")
    snoozed = _snoozed_map(conn, hid, now_iso)
    out = []
    for s in cycles.suggest(purchase_history(conn, hid), now):
        if s["catalog_id"] in on_list or s["catalog_id"] in snoozed:
            continue
        row = conn.execute(
            "SELECT display_name, aliases_json FROM item_catalog WHERE id=?", (s["catalog_id"],)
        ).fetchone()
        out.append({**s, "name": row["display_name"],
                    "name_en": name_en(row["aliases_json"], row["display_name"])})
    return out


def state(conn: sqlite3.Connection, hid: str, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    items = []
    for r in conn.execute(
        """SELECT i.id, c.display_name AS name, c.aliases_json,
                  COALESCE(o.category, c.category) AS category,
                  i.qty_note, i.added_by, i.added_at
           FROM items i JOIN item_catalog c ON c.id = i.catalog_id
           LEFT JOIN catalog_category_override o
                  ON o.household_id = i.household_id AND o.catalog_id = i.catalog_id
           WHERE i.household_id=?
           ORDER BY COALESCE(o.category, c.category, 'zzz'), i.added_at""",
        (hid,),
    ):
        d = dict(r)
        d["name_en"] = name_en(d.pop("aliases_json"), d["name"])
        items.append(d)
    import catalog
    import plants as plantvocab

    week = catalog.weekly_plants(conn, hid, now)
    return {
        "revision": get_revision(conn, hid),
        "items": items,
        "suggestions": suggestions(conn, hid, now),
        # count is plant points per plants.COUNTING_MODE — currently "agp": a flat
        # count of distinct plant species (the study's own method, no fractions).
        # Under "rossi" it becomes fractional (herbs/spices ¼). `weights` carries
        # any non-1.0 token so the panel can mark it; it is empty under AGP.
        "plants": {
            "count": plantvocab.score(week),
            "target": 30,
            "week": week,
            "weights": {t: plantvocab.weight(t) for t in week
                        if plantvocab.weight(t) != 1.0},
        },
    }


# ---- idempotency ledger (scoped by household for defense-in-depth) ----

def prune_applied_ops(conn: sqlite3.Connection, cutoff_iso: str) -> None:
    conn.execute("DELETE FROM applied_ops WHERE applied_at < ?", (cutoff_iso,))


def record_op(conn, hid: str, op_id: str, applied_at: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO applied_ops(op_id, household_id, applied_at, result_json) VALUES(?,?,?,?)",
        (op_id, hid, applied_at, json.dumps(result, ensure_ascii=False)),
    )


def get_applied(conn, hid: str, op_id: str) -> dict | None:
    row = conn.execute(
        "SELECT result_json FROM applied_ops WHERE op_id=? AND household_id=?", (op_id, hid)
    ).fetchone()
    return json.loads(row["result_json"]) if row else None


def set_snooze(conn, hid: str, catalog_id: int, until_iso: str) -> None:
    conn.execute(
        "INSERT INTO catalog_snooze(household_id, catalog_id, snoozed_until) VALUES(?,?,?) "
        "ON CONFLICT(household_id, catalog_id) DO UPDATE SET snoozed_until=excluded.snoozed_until",
        (hid, catalog_id, until_iso),
    )


def delete_user(conn, user_id: str) -> None:
    """GDPR / Apple 5.1.1(v): purge the user and any household they solely own.

    A household with other members survives (their data is shared); a household
    where this user is the last member is deleted with all its scoped state.
    """
    hids = [r["household_id"] for r in conn.execute(
        "SELECT household_id FROM household_members WHERE user_id=?", (user_id,))]
    conn.execute("DELETE FROM household_members WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    for hid in hids:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM household_members WHERE household_id=?", (hid,)).fetchone()[0]
        if remaining == 0:
            for tbl in ("items", "purchase_events", "catalog_snooze",
                        "catalog_category_override", "applied_ops"):
                conn.execute(f"DELETE FROM {tbl} WHERE household_id=?", (hid,))
            conn.execute("DELETE FROM households WHERE id=?", (hid,))
    conn.commit()
