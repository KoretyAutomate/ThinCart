"""
db.py — ThinCart SQLite layer (WAL). One DB file, server is the source of truth.

Concurrency model: FastAPI is async but ops are tiny; a single connection guarded
by an asyncio.Lock in app.py serializes all writes. SQLite WAL keeps readers cheap.
"""

import contextlib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import away_db
import emoji
from datetime import UTC

DB_PATH = Path(os.environ.get("THINCART_DB", Path(__file__).parent / "data" / "thincart.db"))

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
  verified INTEGER NOT NULL DEFAULT 1,   -- 0 = typo-suspect: hidden from candidates
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

CREATE TABLE IF NOT EXISTS stores(
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- AUTOINCREMENT: rowid reuse + offline
                                         -- cross-phone delete could hit the wrong store
  name TEXT NOT NULL,                    -- display, as first typed
  canonical_name TEXT UNIQUE NOT NULL,
  notes TEXT NOT NULL DEFAULT ''         -- "cheap produce, good fish"
);

-- Days the household was out of town (PLAN.md §Intelligence layer 1b). Detection
-- from Google Calendar writes status='auto'; the user's own confirm/reject is a
-- decision a later sync must never overwrite.
-- the last price answer per item (PLAN.md Phase 8): shown only while its
-- input_key still matches the item's current question (price_reco.py)
CREATE TABLE IF NOT EXISTS price_reco(
  catalog_id INTEGER PRIMARY KEY REFERENCES item_catalog(id) ON DELETE CASCADE,
  store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  input_key TEXT NOT NULL,
  answer_json TEXT NOT NULL,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS away_days(
  day TEXT PRIMARY KEY,                  -- YYYY-MM-DD, home-local date
  status TEXT NOT NULL DEFAULT 'auto' CHECK(status IN ('auto','confirmed','rejected')),
  source TEXT NOT NULL DEFAULT 'calendar' CHECK(source IN ('calendar','manual')),
  event_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS applied_ops(
  op_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}'  -- what undo needs (event_id, item snapshot)
);

CREATE INDEX IF NOT EXISTS idx_events_catalog ON purchase_events(catalog_id, bought_at);
CREATE INDEX IF NOT EXISTS idx_items_catalog ON items(catalog_id);

-- Phase 6. WHICH product the household actually buys for a catalog item —
-- "milk" is not a thing you can price, "Wegmans Organic Creamy Sunflower Butter
-- 16oz" is. Chosen once from the chain's own catalogue, then reused, so the
-- question is asked per product rather than per shopping trip.
CREATE TABLE IF NOT EXISTS product_picks(
  catalog_id INTEGER NOT NULL REFERENCES item_catalog(id) ON DELETE CASCADE,
  chain TEXT NOT NULL,                   -- which chain's catalogue this sku is from
  sku TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL DEFAULT '',
  pack_size TEXT NOT NULL DEFAULT '',
  picked_at TEXT NOT NULL,
  PRIMARY KEY (catalog_id, chain)
);

-- Phase 6. Everything any outbound lookup has ever learned, with the date it
-- learned it. Read before the network is asked anything, which bounds how often
-- the shopping list is described to anyone outside the tailnet. Disposable:
-- deleting a row costs one re-fetch, never a fact the household typed.
CREATE TABLE IF NOT EXISTS lookup_cache(
  kind TEXT NOT NULL,                    -- store | product | aisle | price
  key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (kind, key)
);

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
    # migration for DBs created before the typo-verification column
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE item_catalog ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
    # migration for DBs created before per-item emoji icons
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE item_catalog ADD COLUMN emoji TEXT")
    # migrations for DBs created before stores / notes / purchase criteria (Phase 5)
    for ddl in (
        "ALTER TABLE item_catalog ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE item_catalog ADD COLUMN budget REAL",
        "ALTER TABLE item_catalog ADD COLUMN preferred_store_id INTEGER REFERENCES stores(id)",
        "ALTER TABLE purchase_events ADD COLUMN store_id INTEGER REFERENCES stores(id)",
    ):
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(ddl)
    # migrations for DBs created before stores were pinned to a real place (Phase 6).
    # A store with no osm_id is still a perfectly good store — free text stays a
    # first-class way to add one, for shops OpenStreetMap has never heard of.
    for ddl in (
        "ALTER TABLE stores ADD COLUMN osm_id TEXT",
        "ALTER TABLE stores ADD COLUMN address TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE stores ADD COLUMN lat REAL",
        "ALTER TABLE stores ADD COLUMN lon REAL",
        "ALTER TABLE stores ADD COLUMN brand TEXT NOT NULL DEFAULT ''",
        # The chain's OWN id for this branch (Wegmans Princeton = "93"). Distinct
        # from osm_id: that pins the store on a map, this is what the chain's
        # product data is keyed by. A store with no chain simply has no prices
        # and no aisles, which is the normal case and must read as such.
        "ALTER TABLE stores ADD COLUMN chain TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE stores ADD COLUMN chain_store_id TEXT NOT NULL DEFAULT ''",
        # When the emoji backfill last ASKED about this row, whatever the answer.
        # Without it the backfill re-selects the same unfillable rows every run
        # (gibberish names the LLM rightly declines) and never reaches the rest.
        "ALTER TABLE item_catalog ADD COLUMN emoji_tried_at TEXT",
        # Every calendar that currently proposes this away day (JSON list of ids).
        # event_id keeps only the first claimant's details for display; pruning
        # needs them all, or a hidden calendar's claim dies with a visible one's.
        "ALTER TABLE away_days ADD COLUMN claims TEXT NOT NULL DEFAULT '[]'",
        # Phase 7: a standing brand preference; '' means any brand will do.
        "ALTER TABLE item_catalog ADD COLUMN brand TEXT NOT NULL DEFAULT ''",
        # Phase 8: how much the owner wants to buy ("2 lb"); '' = no preference
        "ALTER TABLE item_catalog ADD COLUMN buy_qty TEXT NOT NULL DEFAULT ''",
    ):
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(ddl)
    # Organic was briefly a per-item column (2026-09-26, never merged); it is a
    # household setting now (meta 'organic'). Drop the column where it exists.
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE item_catalog DROP COLUMN organic")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('organic', '0')")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('revision', '0')")
    conn.commit()
    return conn


def bump_revision(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='revision' RETURNING CAST(value AS INTEGER)"
    )
    return cur.fetchone()[0]


def get_revision(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])


def get_or_create_catalog(conn: sqlite3.Connection, name: str) -> int:
    canon = canonical(name)
    row = conn.execute("SELECT id FROM item_catalog WHERE canonical_name=?", (canon,)).fetchone()
    if row:
        return row["id"]
    # alias match: typed "milk" / "たまご" must land on the 牛乳 / 卵 row
    for r in conn.execute("SELECT id, aliases_json FROM item_catalog WHERE aliases_json != '[]'"):
        if any(canonical(a) == canon for a in json.loads(r["aliases_json"])):
            return r["id"]
    # curated emoji is instant/offline for common items; the LLM enrichment
    # (catalog.enrich) fills one for anything not in the map.
    cur = conn.execute(
        "INSERT INTO item_catalog(canonical_name, display_name, emoji) VALUES(?, ?, ?)",
        (canon, name.strip(), emoji.lookup(canon)),
    )
    if cur.lastrowid is None:  # pragma: no cover - sqlite always sets it on INSERT
        # Every caller uses the result immediately as a catalog id. Returning
        # None from a function typed `-> int` would push a null catalog_id into
        # a column that accepts it, so the row saves and the failure only
        # surfaces later as an item that belongs to no catalog entry. Fail here,
        # where the cause is still legible.
        raise RuntimeError(f"INSERT into item_catalog gave no rowid for {canon!r}")
    return cur.lastrowid


def get_or_create_store(conn: sqlite3.Connection, name: str) -> int | None:
    """Store id by canonical name — get-or-create, NEVER a bare INSERT: two
    spellings that canonicalize identically ("OK Store" / "ok　store") must land
    on one row, or the UNIQUE constraint 500s and wedges the client op queue."""
    canon = canonical(name)
    if not canon:
        return None
    row = conn.execute("SELECT id FROM stores WHERE canonical_name=?", (canon,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO stores(name, canonical_name) VALUES(?, ?)", (name.strip(), canon))
    return cur.lastrowid


def stores_list(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, notes, osm_id, address, lat, lon, brand, chain, chain_store_id "
            "FROM stores ORDER BY name"
        )
    ]


def history_stores(conn: sqlite3.Connection) -> dict[int, int]:
    """catalog_id -> the store it was bought at most often (tie: most recently)."""
    hist: dict[int, int] = {}
    for r in conn.execute(
        """SELECT catalog_id, store_id, COUNT(*) AS n, MAX(bought_at) AS last
           FROM purchase_events WHERE store_id IS NOT NULL
           GROUP BY catalog_id, store_id ORDER BY n, last"""
    ):  # ascending order + dict overwrite → the (max n, latest) row wins
        hist[r["catalog_id"]] = r["store_id"]
    return hist


def recommended_stores(conn: sqlite3.Connection, prices: dict[int, dict] | None = None) -> dict[int, tuple[int, str]]:
    """catalog_id -> (store_id, source). The owner's explicit pick wins; then
    the cheapest store from the last price answer still current for the item
    (PLAN.md Phase 8.1); else the store it was bought at most often."""
    rec: dict[int, tuple[int, str]] = {cid: (sid, "history") for cid, sid in history_stores(conn).items()}
    for cid, answer in (prices or {}).items():
        rec[cid] = (answer["cheapest"]["store_id"], "price")
    for r in conn.execute("SELECT id, preferred_store_id FROM item_catalog WHERE preferred_store_id IS NOT NULL"):
        rec[r["id"]] = (r["preferred_store_id"], "preferred")
    return rec


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


def purchase_history(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """catalog_id -> ordered purchase timestamps (the cycle-estimator substrate)."""
    hist: dict[int, list[str]] = {}
    for r in conn.execute("SELECT catalog_id, bought_at FROM purchase_events ORDER BY bought_at"):
        hist.setdefault(r["catalog_id"], []).append(r["bought_at"])
    return hist


def recent_history(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Recent purchase events, newest first, joined to catalog for display.

    The substrate for the History panel: a mis-swipe logs a spurious
    purchase_event that the ~8 s undo toast can no longer reach once it's gone,
    so the panel exposes each event (by its server id) for after-the-fact repair.
    """
    out = []
    for r in conn.execute(
        """SELECT e.id AS event_id, e.catalog_id, e.bought_at, e.bought_by,
                  c.display_name AS name, c.aliases_json
           FROM purchase_events e JOIN item_catalog c ON c.id = e.catalog_id
           ORDER BY e.bought_at DESC, e.id DESC
           LIMIT ?""",
        (limit,),
    ):
        out.append(
            {
                "event_id": r["event_id"],
                "catalog_id": r["catalog_id"],
                "name": r["name"],
                "name_en": name_en(r["aliases_json"], r["name"]),
                "bought_at": r["bought_at"],
                "bought_by": r["bought_by"],
            }
        )
    return out


def suggestions(conn: sqlite3.Connection, now) -> list[dict]:
    """Due items (cycles.suggest) minus already-listed and snoozed catalog rows."""
    import cycles

    on_list = {r["catalog_id"] for r in conn.execute("SELECT catalog_id FROM items")}
    now_iso = now.isoformat(timespec="seconds")
    out = []
    for s in cycles.suggest(purchase_history(conn), now, away_db.away_set(conn)):
        if s["catalog_id"] in on_list:
            continue
        row = conn.execute(
            "SELECT display_name, aliases_json, snoozed_until FROM item_catalog WHERE id=?",
            (s["catalog_id"],),
        ).fetchone()
        if row["snoozed_until"] and row["snoozed_until"] > now_iso:
            continue
        out.append({**s, "name": row["display_name"], "name_en": name_en(row["aliases_json"], row["display_name"])})
    return out


# "organic onion" is the Onion — one item, one history — whatever the household
# setting. A LEADING qualifier only: mid-name "organic" belongs to a product's
# own name ("simple mills organic seed flour crackers") and is left alone.
# The boundary after "organic" is spelled out — not \b — because Python's \b is
# Unicode-aware and JavaScript's is ASCII-only: "organic卵" must be ONE word on
# both sides (the phone mirrors this in splitOrganic, app/index.html).
# Brands whose NAME begins with "Organic" are not a qualifier: "Organic Valley
# Milk" is that brand's milk, and stripping would store "Valley Milk" (Codex
# review 2026-09-28). Mirrored in app/index.html ORGANIC_RE; the shared cases in
# tests/fixtures/split_organic_cases.json keep the two sides in step.
_ORGANIC_BRANDS = r"valley|girl|india|prairie|traditions"
_ORGANIC_PREFIX = re.compile(
    rf"^\s*(?:organic(?!\w)(?!\s+(?:{_ORGANIC_BRANDS})(?!\w))|オーガニック|有機)[\s・]*",
    re.IGNORECASE,
)


def split_organic(name: str) -> tuple[str, bool]:
    """("organic onion" → ("onion", True)); a name that is only the qualifier
    is not split — "Organic" alone stays an item called Organic."""
    folded = unicodedata.normalize("NFKC", name or "")
    m = _ORGANIC_PREFIX.match(folded)
    if not m:
        return name, False
    base = folded[m.end():].strip()
    return (base, True) if base else (name, False)


def _picks_by_chain(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT catalog_id, chain, sku FROM product_picks ORDER BY catalog_id, chain"):
        out.setdefault(str(r["catalog_id"]), {})[r["chain"]] = r["sku"]
    return out


def _price_brief(answer: dict | None, store_names: dict) -> dict | None:
    """What the list needs from a stored price answer: the cheapest store and
    how it was decided ("$0.21/oz", "2 × 1 lb = $5.98"), with its age."""
    if not answer or not answer.get("cheapest"):
        return None
    c = answer["cheapest"]
    store = store_names.get(c["store_id"])
    if store is None:
        return None
    return {"store": store, "amount": c["amount"], "product": c["product"],
            "unit_label": c.get("unit_label", ""), "total_label": c.get("total_label", ""),
            "exact": c.get("exact", False), "computed_at": answer.get("computed_at", ""),
            # when the store's price was FETCHED — a comparison over a cached
            # quote is not a fresh price, and must not read as one
            "fetched_at": c.get("fetched_at", ""),
            # some store could not be checked when this was decided
            "partial": "unasked" in (answer.get("stores") or {}).values()}


def organic_setting(conn: sqlite3.Connection) -> bool:
    """The household buys organic where it can (PLAN.md Phase 7A, revised)."""
    row = conn.execute("SELECT value FROM meta WHERE key='organic'").fetchone()
    return bool(row and row[0] == "1")


def state(conn: sqlite3.Connection, now=None) -> dict:
    """Full list state — small enough (tens of items) to always send whole."""
    from datetime import datetime

    now = now or datetime.now(UTC)
    stores = stores_list(conn)
    store_names = {s["id"]: s["name"] for s in stores}
    import price_reco
    import quantity

    prices = price_reco.current(conn)
    priced = price_reco.priced_stores(conn)
    rec = recommended_stores(conn, prices)
    hist = history_stores(conn)
    items = []
    for r in conn.execute(
        """SELECT i.id, i.catalog_id, c.display_name AS name, c.aliases_json,
                  c.category, c.emoji, c.note, c.budget, c.brand, c.buy_qty,
                  i.qty_note, i.added_by, i.added_at
           FROM items i JOIN item_catalog c ON c.id = i.catalog_id
           ORDER BY COALESCE(c.category, 'zzz'), i.added_at"""
    ):
        d = dict(r)
        d["name_en"] = name_en(d.pop("aliases_json"), d["name"])
        sid, source = rec.get(d["catalog_id"], (None, None))
        d["store"] = store_names.get(sid)
        d["store_source"] = source if d["store"] else None
        # the phone needs these to fall back by itself (offline): your pick →
        # price → history, and to show the price beside your pick
        d["history_store"] = store_names.get(hist.get(d["catalog_id"]))
        d["buy_qty_ok"] = not d["buy_qty"] or quantity.parse_wanted(d["buy_qty"]) is not None
        # the exact question a price answer must have answered — the phone keys
        # its open price view on it, so a change only the server can see (an
        # enrichment making the item food, under 🍃) is still a new question
        d["price_key"] = price_reco.input_key(conn, d["catalog_id"], priced)
        d["price"] = _price_brief(prices.get(d["catalog_id"]), store_names)
        items.append(d)
    import catalog
    import plants as plantvocab

    week = catalog.weekly_plants(conn, now)
    return {
        "revision": get_revision(conn),
        "items": items,
        # household-wide preferences, shared by both phones
        "settings": {"organic": organic_setting(conn)},
        "stores": stores,
        # catalog_id -> sku of the product the household settled on. Small, and
        # it has to be SYNCED: the other phone choosing a specific jar changes
        # which aisle this phone should be showing, and without it here that
        # change is invisible until the app is reopened.
        "picks": {
            str(r["catalog_id"]): r["sku"]
            for r in conn.execute("SELECT catalog_id, sku FROM product_picks")
        },
        # every chain's pick, not one per item: "by price" asks each chain about
        # ITS pick, so a change at any chain is a new question for the phone
        "picks_by_chain": _picks_by_chain(conn),
        "suggestions": suggestions(conn, now),
        # badge on the Travel button: detected days nobody has ruled on yet
        "away_pending": conn.execute("SELECT COUNT(*) FROM away_days WHERE status='auto'").fetchone()[0],
        # count is plant points per plants.COUNTING_MODE — currently "agp": a flat
        # count of distinct plant species (the study's own method, no fractions).
        # Under "rossi" it becomes fractional (herbs/spices ¼). `weights` carries
        # any non-1.0 token so the panel can mark it; it is empty under AGP.
        "plants": {
            "count": plantvocab.score(week),
            "target": 30,
            "week": week,
            "weights": {t: plantvocab.weight(t) for t in week if plantvocab.weight(t) != 1.0},
        },
    }


def prune_applied_ops(conn: sqlite3.Connection, cutoff_iso: str) -> None:
    conn.execute("DELETE FROM applied_ops WHERE applied_at < ?", (cutoff_iso,))


def record_op(conn: sqlite3.Connection, op_id: str, applied_at: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO applied_ops(op_id, applied_at, result_json) VALUES(?,?,?)",
        (op_id, applied_at, json.dumps(result, ensure_ascii=False)),
    )


def get_applied(conn: sqlite3.Connection, op_id: str) -> dict | None:
    row = conn.execute("SELECT result_json FROM applied_ops WHERE op_id=?", (op_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None
