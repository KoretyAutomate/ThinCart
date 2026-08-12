"""
app.py — ThinCart server: static PWA + REST ops + WebSocket broadcast.

Sync contract (PLAN.md §Architecture):
- Mutations arrive ONLY via POST /api/op (retryable HTTP; store Wi-Fi drops WS
  constantly). Every op carries a client UUID op_id; replays are silently
  re-ACKed from the applied_ops ledger — a checkoff replayed twice logs ONE event.
- The WS at /ws is downstream-only: after every applied op the FULL state is
  broadcast to all sockets (list is tens of items — full-state beats delta-merge).
- Ops targeting a vanished item id are no-op ACKs (settles checkoff-vs-remove).
- Add is idempotent per NFKC-canonical name: two phones adding "milk" offline
  converge to one row.

Run (tailnet-bound — bind the Tailscale IP, NOT 0.0.0.0):
    uvicorn app:app --host 100.112.171.54 --port 8123 --no-access-log
"""

import asyncio
import json
import logging
import unicodedata
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import away
import catalog
import cycles
import db
import ideas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("thincart")

APP_DIR = Path(__file__).parent.parent / "app"

app = FastAPI(title="ThinCart", version="0.1")

conn = db.connect()
write_lock = asyncio.Lock()  # serializes all mutations on the single connection
sockets: set[WebSocket] = set()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Feature modules own their own endpoints; they borrow the shared connection and
# broadcast through bind() rather than importing app, which would be circular.
# Routers are included at the bottom, after broadcast_state exists.
ideas.bind(conn)


class Op(BaseModel):
    op_id: str = Field(..., min_length=8, max_length=64)
    type: Literal[
        "add",
        "checkoff",
        "remove",
        "skip",
        "undo_checkoff",
        "undo_purchase",
        "snooze",
        "edit",
        "store_upsert",
        "store_delete",
    ]
    actor: str = Field("", max_length=40)
    # add / edit
    name: str | None = Field(None, max_length=120)
    qty_note: str | None = Field(None, max_length=120)
    # edit (category adjustment — one of catalog.CATEGORIES)
    category: str | None = Field(None, max_length=20)
    # edit: persistent purchase criteria (catalog-level — survive checkoff→re-add)
    note: str | None = Field(None, max_length=200)
    # edit: typical price. STRING on the wire: "" clears; "３００円"/"¥1,200" parse
    budget: str | None = Field(None, max_length=20)
    # edit / checkoff: store display name ("" clears the preference on edit)
    store: str | None = Field(None, max_length=60)
    # store_upsert
    store_name: str | None = Field(None, max_length=60)
    store_notes: str | None = Field(None, max_length=300)
    # store_delete
    store_id: int | None = None
    # add (client-generated item uuid) / checkoff / remove
    item_id: str | None = Field(None, min_length=8, max_length=64)
    # undo_checkoff: the op_id of the checkoff being undone
    target_op_id: str | None = Field(None, max_length=64)
    # undo_purchase: the purchase_events.id being corrected from the History panel
    event_id: int | None = None
    # snooze (suggestion dismissal — server-side so it silences BOTH phones)
    # edit fallback: lets criteria apply when the item row vanished mid-edit
    catalog_id: int | None = None


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


async def broadcast_state() -> None:
    payload = json.dumps(db.state(conn), ensure_ascii=False)
    dead = []
    for ws in sockets:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def apply_add(op: Op, ts: str) -> dict:
    if not op.name or not op.name.strip():
        raise HTTPException(422, "add requires a non-empty name")
    catalog_id = db.get_or_create_catalog(conn, op.name)
    existing = conn.execute("SELECT id FROM items WHERE catalog_id=?", (catalog_id,)).fetchone()
    if existing:  # duplicate-add convergence: already on the list → no-op
        return {"item_id": existing["id"], "deduped": True}
    item_id = op.item_id or str(uuid.uuid4())
    rev = db.bump_revision(conn)
    conn.execute(
        "INSERT INTO items(id, catalog_id, qty_note, added_by, added_at, revision) VALUES(?,?,?,?,?,?)",
        (item_id, catalog_id, op.qty_note or "", op.actor, ts, rev),
    )
    return {"item_id": item_id, "catalog_id": catalog_id}


def apply_checkoff(op: Op, ts: str) -> dict:
    if op.item_id is None:
        raise HTTPException(422, "checkoff requires item_id")
    row = conn.execute(
        "SELECT id, catalog_id, qty_note, added_by, added_at FROM items WHERE id=?",
        (op.item_id,),
    ).fetchone()
    if row is None:  # already checked off / removed by the other phone
        return {"noop": True}
    conn.execute("DELETE FROM items WHERE id=?", (op.item_id,))
    # "I'm at: <store>" on the client stamps where this was bought — the ground
    # truth behind the where-to-buy recommendation (db.recommended_stores)
    store_id = db.get_or_create_store(conn, op.store) if op.store else None
    cur = conn.execute(
        "INSERT INTO purchase_events(catalog_id, bought_at, bought_by, store_id) VALUES(?,?,?,?)",
        (row["catalog_id"], ts, op.actor, store_id),
    )
    db.bump_revision(conn)
    # snapshot everything undo needs to resurrect the item + kill the event
    return {
        "event_id": cur.lastrowid,
        "item": {k: row[k] for k in ("catalog_id", "qty_note", "added_by", "added_at")},
    }


def apply_remove(op: Op, ts: str) -> dict:
    if op.item_id is None:
        raise HTTPException(422, "remove requires item_id")
    cur = conn.execute("DELETE FROM items WHERE id=?", (op.item_id,))
    if cur.rowcount == 0:
        return {"noop": True}
    db.bump_revision(conn)
    return {"removed": op.item_id}


def apply_skip(op: Op, ts: str) -> dict:
    """Out of stock: off the list, NO purchase event (interval data stays clean),
    and only a 1-day suggestion snooze so the tray re-suggests it next trip."""
    if op.item_id is None:
        raise HTTPException(422, "skip requires item_id")
    row = conn.execute("SELECT catalog_id FROM items WHERE id=?", (op.item_id,)).fetchone()
    if row is None:
        return {"noop": True}
    conn.execute("DELETE FROM items WHERE id=?", (op.item_id,))
    until = (datetime.fromisoformat(ts) + timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE item_catalog SET snoozed_until=? WHERE id=?", (until, row["catalog_id"]))
    db.bump_revision(conn)
    return {"skipped": op.item_id, "resuggest_after": until}


def apply_undo_checkoff(op: Op, ts: str) -> dict:
    """Fat-finger repair: delete the purchase_event, put the item back."""
    if not op.target_op_id:
        raise HTTPException(422, "undo_checkoff requires target_op_id")
    target = db.get_applied(conn, op.target_op_id)
    if not target or "event_id" not in target:  # unknown / was a no-op → nothing to undo
        return {"noop": True}
    conn.execute("DELETE FROM purchase_events WHERE id=?", (target["event_id"],))
    snap = target["item"]
    rev = db.bump_revision(conn)
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items(id, catalog_id, qty_note, added_by, added_at, revision) VALUES(?,?,?,?,?,?)",
        (item_id, snap["catalog_id"], snap["qty_note"], snap["added_by"], snap["added_at"], rev),
    )
    return {"item_id": item_id, "undone_event": target["event_id"]}


def apply_undo_purchase(op: Op, ts: str) -> dict:
    """History-panel mis-swipe repair, keyed by the server event id so ANY past
    purchase can be corrected — not just one still inside the 7-day op ledger
    (undo_checkoff's limit). Deletes the purchase_event and puts the item back
    on the list so it isn't silently lost."""
    if op.event_id is None:
        raise HTTPException(422, "undo_purchase requires event_id")
    row = conn.execute("SELECT catalog_id FROM purchase_events WHERE id=?", (op.event_id,)).fetchone()
    if row is None:  # already undone (double-tap / the other phone) → nothing to do
        return {"noop": True}
    conn.execute("DELETE FROM purchase_events WHERE id=?", (op.event_id,))
    catalog_id = row["catalog_id"]
    rev = db.bump_revision(conn)
    existing = conn.execute("SELECT id FROM items WHERE catalog_id=?", (catalog_id,)).fetchone()
    if existing:  # already back on the list → the event deletion alone stands
        return {"undone_event": op.event_id, "item_id": existing["id"], "deduped": True}
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items(id, catalog_id, qty_note, added_by, added_at, revision) VALUES(?,?,?,?,?,?)",
        (item_id, catalog_id, "", op.actor, ts, rev),
    )
    return {"undone_event": op.event_id, "item_id": item_id}


def apply_edit(op: Op, ts: str) -> dict:
    """Long-press editor: rename + quantity note (per-item), plus catalog-level
    fields — category, note (purchase criteria), budget, preferred store.
    A rename re-points items.catalog_id at get_or_create_catalog(new name) —
    never rewrites the old catalog row, whose name/criteria stay with the old
    concept. Renaming onto a name already on the list merges the two rows (the
    survivor keeps its id; qty/criteria from the same save follow). Catalog-
    level edits resolve through op.catalog_id when the item row vanished
    mid-edit (spouse checked it off while the sheet was open): criteria must
    not be silently lost — persisting across checkoffs is their whole point."""
    if op.item_id is None and op.catalog_id is None:
        raise HTTPException(422, "edit requires item_id or catalog_id")
    row = None
    if op.item_id is not None:
        row = conn.execute("SELECT catalog_id FROM items WHERE id=?", (op.item_id,)).fetchone()
    catalog_id = row["catalog_id"] if row else op.catalog_id
    if catalog_id is None:  # item gone and no catalog fallback → nothing to edit
        return {"noop": True}
    changed = False
    item_id = op.item_id
    result: dict = {}
    if op.name is not None and op.name.strip() and row is not None:
        new_cid = db.get_or_create_catalog(conn, op.name)
        if new_cid != catalog_id:
            dup = conn.execute(
                "SELECT id FROM items WHERE catalog_id=? AND id != ?",
                (new_cid, op.item_id),
            ).fetchone()
            if dup:  # renamed onto an existing list entry → converge
                conn.execute("DELETE FROM items WHERE id=?", (op.item_id,))
                item_id = dup["id"]
                result["merged_into"] = item_id
            else:
                conn.execute("UPDATE items SET catalog_id=? WHERE id=?", (new_cid, item_id))
            catalog_id = new_cid  # same-save criteria land on the NEW concept
            result["catalog_id"] = new_cid
            changed = True
        else:
            # get_or_create_catalog resolved back to the item's OWN row, for one
            # of two very different reasons — they must not be treated alike.
            cur = conn.execute(
                "SELECT canonical_name, display_name FROM item_catalog WHERE id=?",
                (catalog_id,),
            ).fetchone()
            if cur["canonical_name"] == db.canonical(op.name):
                # (1) Canonical variant: only case / full-width / whitespace
                # differ, so this is the SAME concept respelled — the user
                # fixing how it reads. display_name is the only place that
                # spelling lives (it is written at INSERT and nowhere else), so
                # without this the correction is silently discarded on the next
                # fetch. Applied even when other items share this catalog row:
                # canonical_name is unchanged, so every sharer is by definition
                # the same concept and wants the corrected spelling too.
                if cur["display_name"] != op.name.strip():
                    conn.execute("UPDATE item_catalog SET display_name=? WHERE id=?", (op.name.strip(), catalog_id))
                    changed = True
                result["name"] = op.name.strip()
            else:
                # (2) Alias match ("milk" → the 牛乳 row). Renaming the shared
                # row here would rename the concept for every item and every
                # phone that reaches it through any other spelling, from an edit
                # the user thinks is local. Splitting off a new row instead
                # would defeat the alias feature itself. So the rename is
                # refused — but reported, never silently swallowed, so the
                # client can roll its optimistic text back to `name`.
                result["rename_skipped"] = "alias"
                result["name"] = cur["display_name"]
    if op.qty_note is not None and row is not None:  # per-item: needs the live row
        conn.execute("UPDATE items SET qty_note=? WHERE id=?", (op.qty_note, item_id))
        changed = True
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
    if changed:
        db.bump_revision(conn)
    return {"edited": op.item_id or catalog_id, "changed": changed, **result}


def apply_snooze(op: Op, ts: str) -> dict:
    """Dismiss a suggestion: snooze for ½ its median cycle (PLAN.md), min 2 days.

    The half-cycle is in in-town days, so the deadline is walked forward with
    `Away.advance` — a snooze must not burn away while nobody is home to shop.
    """
    if op.catalog_id is None:
        raise HTTPException(422, "snooze requires catalog_id")
    hist = db.purchase_history(conn).get(op.catalog_id, [])
    away = db.away_set(conn)
    # a potential item's own gap is a better snooze basis than the 7-day
    # default; only a bought-once item has nothing of its own to go on
    cycle = cycles.estimate(hist, away).cycle
    half = (cycle if cycle is not None else 7.0) / 2
    until = away.advance(datetime.fromisoformat(ts), max(half, 2.0)).isoformat(timespec="seconds")
    cur = conn.execute("UPDATE item_catalog SET snoozed_until=? WHERE id=?", (until, op.catalog_id))
    if cur.rowcount == 0:
        return {"noop": True}
    db.bump_revision(conn)
    return {"snoozed_until": until}


def apply_store_upsert(op: Op, ts: str) -> dict:
    """Stores panel: create a store / update its notes. Notes feed nothing
    automated yet — they are the household's shared knowledge of each store."""
    if not op.store_name or not op.store_name.strip():
        raise HTTPException(422, "store_upsert requires store_name")
    sid = db.get_or_create_store(conn, op.store_name)
    if op.store_notes is not None:
        conn.execute("UPDATE stores SET notes=? WHERE id=?", (op.store_notes.strip(), sid))
    db.bump_revision(conn)
    return {"store_id": sid}


def apply_store_delete(op: Op, ts: str) -> dict:
    """Typo repair. Nulls references (preference + bought-at history) rather than
    orphaning them. A lagging offline op naming this store re-creates it —
    documented, acceptable (PLAN.md Phase 5 review delta 7)."""
    if op.store_id is None:
        raise HTTPException(422, "store_delete requires store_id")
    row = conn.execute("SELECT id FROM stores WHERE id=?", (op.store_id,)).fetchone()
    if row is None:  # already deleted (double-tap / other phone) → no-op
        return {"noop": True}
    conn.execute("UPDATE item_catalog SET preferred_store_id=NULL WHERE preferred_store_id=?", (op.store_id,))
    conn.execute("UPDATE purchase_events SET store_id=NULL WHERE store_id=?", (op.store_id,))
    conn.execute("DELETE FROM stores WHERE id=?", (op.store_id,))
    db.bump_revision(conn)
    return {"deleted_store": op.store_id}


APPLY = {
    "add": apply_add,
    "checkoff": apply_checkoff,
    "remove": apply_remove,
    "skip": apply_skip,
    "undo_checkoff": apply_undo_checkoff,
    "undo_purchase": apply_undo_purchase,
    "snooze": apply_snooze,
    "edit": apply_edit,
    "store_upsert": apply_store_upsert,
    "store_delete": apply_store_delete,
}


async def enrich_and_push(catalog_id: int) -> None:
    """Fire-and-forget add-time enrichment; broadcast if categories/plants changed."""
    try:
        if await catalog.enrich(conn, write_lock, catalog_id):
            await broadcast_state()
    except Exception:
        log.exception("enrichment failed for catalog_id=%s", catalog_id)


async def enrich_sweeper() -> None:
    """Nightly sweep for rows the add-time task missed (LLM was down, etc.)."""
    while True:
        try:
            if await catalog.sweep(conn, write_lock):
                await broadcast_state()
        except Exception:
            log.exception("enrichment sweep failed")
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def startup():
    asyncio.create_task(enrich_sweeper())
    asyncio.create_task(away.sweeper())


@app.post("/api/op")
async def post_op(op: Op):
    async with write_lock:
        prior = db.get_applied(conn, op.op_id)
        if prior is not None:  # replay after a lost ACK → re-ACK, mutate nothing
            return {"ok": True, "replayed": True, "result": prior, "revision": db.get_revision(conn)}
        ts = now_iso()
        result = APPLY[op.type](op, ts)
        db.record_op(conn, op.op_id, ts, result)
        db.prune_applied_ops(conn, (datetime.now(UTC) - timedelta(days=7)).isoformat())
        conn.commit()
    await broadcast_state()
    if op.type in ("add", "edit") and "catalog_id" in result:
        row = conn.execute(
            "SELECT llm_enriched_at FROM item_catalog WHERE id=?",
            (result["catalog_id"],),
        ).fetchone()
        if row and row["llm_enriched_at"] is None:
            asyncio.create_task(enrich_and_push(result["catalog_id"]))
    return {"ok": True, "result": result, "revision": db.get_revision(conn)}


@app.get("/api/state")
async def get_state():
    return db.state(conn)


@app.get("/api/catalog")
async def get_catalog():
    """Typing-candidate corpus: every known item, most-purchased first.
    Client matches locally (instant, kana-folded); refreshed on boot/wake."""
    rows = conn.execute(
        """SELECT c.id, c.display_name AS name, c.category, c.aliases_json,
                  (SELECT COUNT(*) FROM purchase_events e WHERE e.catalog_id=c.id) AS buys
           FROM item_catalog c WHERE c.verified = 1
           ORDER BY buys DESC, c.display_name"""
    ).fetchall()
    return {
        "catalog": [
            {
                "name": r["name"],
                "category": r["category"],
                "name_en": db.name_en(r["aliases_json"], r["name"]),
                "aliases": json.loads(r["aliases_json"]),
            }
            for r in rows
        ]
    }


@app.get("/api/cycles")
async def get_cycles():
    """EVERY item the household has ever bought, with its confidence tier
    (PLAN.md §1c), most-due first.

    Scope is deliberately the whole purchase history, not just the items with a
    learned median: an item bought once is real information, and hiding it made
    the app look like it had forgotten the purchase. What separates the tiers is
    how much is claimed about them, not whether they appear.

    median_days and days_since are **in-town** days (PLAN.md §1b) — the panel
    labels them as such, since "6 days since" reading as 9 calendar days is
    otherwise indistinguishable from a bug.
    """
    now = datetime.now(UTC)
    away = db.away_set(conn)
    now_iso_s = now.isoformat(timespec="seconds")
    on_list = {r["catalog_id"] for r in conn.execute("SELECT catalog_id FROM items")}
    out = []
    for cid, ts in db.purchase_history(conn).items():
        est = cycles.estimate(ts, away)
        row = conn.execute(
            "SELECT display_name, aliases_json, snoozed_until FROM item_catalog WHERE id=?",
            (cid,),
        ).fetchone()
        if row is None:
            continue
        score = est.score(now, away)
        due_in = est.due_in_days(now, away)
        # snoozed / already-listed items keep their numbers but lose their call
        # to action: the panel still shows the rhythm, the tray stays quiet
        silenced = cid in on_list or bool(row["snoozed_until"] and row["snoozed_until"] > now_iso_s)
        tier = None if silenced else est.tier(now, away)
        out.append(
            {
                "catalog_id": cid,
                "name": row["display_name"],
                "name_en": db.name_en(row["aliases_json"], row["display_name"]),
                "tier": tier,
                "trusted": est.trusted,
                "events": est.events,
                # all None for a bought-once item: there is no interval to name
                "weeks": est.weeks,
                "label": cycles.cycle_label(est.cycle_days) if est.has_cycle else None,
                "median_days": round(est.cycle_days, 1) if est.has_cycle else None,
                "spread": round(est.spread, 2) if est.spread is not None else None,
                "days_since": round(away.between(est.last, now), 1) if est.last else None,
                "due_in_days": round(due_in, 1) if due_in is not None else None,
                "score": round(score, 2) if score is not None else None,
                "due": tier is not None,
                "on_list": cid in on_list,
            }
        )
    # buy-now first, then coming-up, then merely tracked, then bought-once
    order = {cycles.HIGH: 0, cycles.POTENTIAL: 1}
    out.sort(key=lambda x: (order.get(x["tier"], 2), x["score"] is None, -(x["score"] or 0)))
    return {
        "cycles": out,
        "away_days": len(away.days),
        "unit": "in-town days",
        "recent_intervals": cycles.RECENT_INTERVALS,
        # in-town days the coming week holds — the horizon POTENTIAL is measured
        # against, and near zero when the household is away for it
        "week_horizon": round(cycles.week_horizon(now, away), 1),
        "tiers": {
            "high": sum(1 for c in out if c["tier"] == cycles.HIGH),
            "potential": sum(1 for c in out if c["tier"] == cycles.POTENTIAL),
            "tracked": sum(1 for c in out if c["tier"] is None and c["median_days"] is not None),
            "once": sum(1 for c in out if c["median_days"] is None),
        },
    }


@app.get("/api/history")
async def get_history(limit: int = 100):
    """Recent purchases, newest first — the History panel that lets a mis-swipe
    be corrected (undo_purchase) long after the ~8 s undo toast is gone."""
    return {"history": db.recent_history(conn, limit)}


@app.get("/health")
async def health():
    return {"ok": True, "revision": db.get_revision(conn), "clients": len(sockets)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        await ws.send_text(json.dumps(db.state(conn), ensure_ascii=False))
        while True:  # downstream-only; reads exist just to detect close/pings
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(ws)


away.bind(away.Context(conn=conn, write_lock=write_lock, broadcast=broadcast_state, now_iso=now_iso))
app.include_router(away.router)
app.include_router(ideas.router)


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "index.html")


app.mount("/", StaticFiles(directory=APP_DIR), name="static")
