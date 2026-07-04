"""
app.py — PlantCart server: static PWA + REST ops + WebSocket broadcast.

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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import catalog
import cycles
import db
import llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("plantcart")

APP_DIR = Path(__file__).parent.parent / "app"

app = FastAPI(title="PlantCart", version="0.1")

conn = db.connect()
write_lock = asyncio.Lock()  # serializes all mutations on the single connection
sockets: set[WebSocket] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Op(BaseModel):
    op_id: str = Field(..., min_length=8, max_length=64)
    type: Literal["add", "checkoff", "remove", "skip", "undo_checkoff", "snooze"]
    actor: str = Field("", max_length=40)
    # add
    name: str | None = Field(None, max_length=120)
    qty_note: str | None = Field(None, max_length=120)
    # add (client-generated item uuid) / checkoff / remove
    item_id: str | None = Field(None, min_length=8, max_length=64)
    # undo_checkoff: the op_id of the checkoff being undone
    target_op_id: str | None = Field(None, max_length=64)
    # snooze (suggestion dismissal — server-side so it silences BOTH phones)
    catalog_id: int | None = None


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
    existing = conn.execute(
        "SELECT id FROM items WHERE catalog_id=?", (catalog_id,)
    ).fetchone()
    if existing:  # duplicate-add convergence: already on the list → no-op
        return {"item_id": existing["id"], "deduped": True}
    item_id = op.item_id or str(uuid.uuid4())
    rev = db.bump_revision(conn)
    conn.execute(
        "INSERT INTO items(id, catalog_id, qty_note, added_by, added_at, revision) "
        "VALUES(?,?,?,?,?,?)",
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
    cur = conn.execute(
        "INSERT INTO purchase_events(catalog_id, bought_at, bought_by) VALUES(?,?,?)",
        (row["catalog_id"], ts, op.actor),
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
    row = conn.execute(
        "SELECT catalog_id FROM items WHERE id=?", (op.item_id,)
    ).fetchone()
    if row is None:
        return {"noop": True}
    conn.execute("DELETE FROM items WHERE id=?", (op.item_id,))
    until = (datetime.fromisoformat(ts) + timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE item_catalog SET snoozed_until=? WHERE id=?", (until, row["catalog_id"])
    )
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
        "INSERT INTO items(id, catalog_id, qty_note, added_by, added_at, revision) "
        "VALUES(?,?,?,?,?,?)",
        (item_id, snap["catalog_id"], snap["qty_note"], snap["added_by"],
         snap["added_at"], rev),
    )
    return {"item_id": item_id, "undone_event": target["event_id"]}


def apply_snooze(op: Op, ts: str) -> dict:
    """Dismiss a suggestion: snooze for ½ its median cycle (PLAN.md), min 2 days."""
    if op.catalog_id is None:
        raise HTTPException(422, "snooze requires catalog_id")
    hist = db.purchase_history(conn).get(op.catalog_id, [])
    half = (cycles.median_interval_days(hist) or 7.0) / 2
    until = (datetime.fromisoformat(ts) + timedelta(days=max(half, 2.0))).isoformat(
        timespec="seconds"
    )
    cur = conn.execute(
        "UPDATE item_catalog SET snoozed_until=? WHERE id=?", (until, op.catalog_id)
    )
    if cur.rowcount == 0:
        return {"noop": True}
    db.bump_revision(conn)
    return {"snoozed_until": until}


APPLY = {
    "add": apply_add,
    "checkoff": apply_checkoff,
    "remove": apply_remove,
    "skip": apply_skip,
    "undo_checkoff": apply_undo_checkoff,
    "snooze": apply_snooze,
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


@app.post("/api/op")
async def post_op(op: Op):
    async with write_lock:
        prior = db.get_applied(conn, op.op_id)
        if prior is not None:  # replay after a lost ACK → re-ACK, mutate nothing
            return {"ok": True, "replayed": True, "result": prior,
                    "revision": db.get_revision(conn)}
        ts = now_iso()
        result = APPLY[op.type](op, ts)
        db.record_op(conn, op.op_id, ts, result)
        db.prune_applied_ops(
            conn, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        )
        conn.commit()
    await broadcast_state()
    if op.type == "add" and "catalog_id" in result:
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
    return {"catalog": [
        {"name": r["name"], "category": r["category"],
         "name_en": db.name_en(r["aliases_json"], r["name"]),
         "aliases": json.loads(r["aliases_json"])}
        for r in rows
    ]}


IDEAS_TTL_H = 6
ideas_cache: dict = {"data": None, "at": None}


def _recipes_prompt(available, on_list, plants) -> str:
    return (
        "You help a Japanese/English bilingual household diversify toward 30 different "
        f"edible plants per week. Plants eaten this week: {json.dumps(plants)}.\n"
        f"Ingredients they have (bought recently): {json.dumps(available, ensure_ascii=False)}.\n"
        f"Already on their shopping list: {json.dumps(on_list, ensure_ascii=False)}.\n"
        'Suggest 3 easy dinner recipes. Reply ONLY JSON: {"recipes": [{'
        '"title": str (English, may add Japanese in parens), '
        '"uses": [ingredients they already have], '
        '"missing": [1-3 grocery items to buy, in the language the ingredient is usually '
        'listed on their list], '
        '"new_plants": [lowercase English plant tokens this adds beyond their week]}]}'
    )


def _diversity_prompt(recent_plants) -> str:
    return (
        f"A household ate these plants recently: {json.dumps(recent_plants)}.\n"
        "Suggest 8 DIFFERENT edible plants, common in Japanese supermarkets, to broaden "
        'their variety toward 30 plants/week. Reply ONLY JSON: {"suggestions": [{'
        '"plant": lowercase English plant token, '
        '"buy": the concrete grocery item to put on the list, in Japanese}]}'
    )


@app.get("/api/ideas")
async def get_ideas(refresh: int = 0):
    """Recipes + diversity suggestions (LLM). Cached; failure never blocks the list."""
    if (
        not refresh
        and ideas_cache["data"]
        and (datetime.now(timezone.utc) - ideas_cache["at"]).total_seconds()
        < IDEAS_TTL_H * 3600
    ):
        return ideas_cache["data"]

    available = [p["name"] for p in catalog.recent_purchases(conn)]
    on_list = [i["name"] for i in db.state(conn)["items"]]
    week = catalog.weekly_plants(conn)
    month = catalog.weekly_plants(conn, window_days=30)

    recipes, diversity = await asyncio.gather(
        llm.chat_json(_recipes_prompt(available, on_list, week), max_tokens=700, timeout=90),
        llm.chat_json(_diversity_prompt(month), max_tokens=400, timeout=90),
    )
    recipes = recipes.get("recipes") if isinstance(recipes, dict) else None
    diversity = diversity.get("suggestions") if isinstance(diversity, dict) else None
    if diversity is not None:
        # the LLM sometimes suggests plants just eaten despite the prompt —
        # enforce "different" deterministically against the 30-day set
        eaten = set(month)
        diversity = [
            s for s in diversity
            if isinstance(s, dict) and s.get("buy")
            and str(s.get("plant", "")).lower() not in eaten
        ]
    if recipes is None and diversity is None:
        raise HTTPException(503, "LLM unavailable — list and sync are unaffected")

    data = {
        "recipes": recipes or [],
        "diversity": diversity or [],
        "generated_at": now_iso(),
    }
    ideas_cache["data"], ideas_cache["at"] = data, datetime.now(timezone.utc)
    return data


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


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "index.html")


app.mount("/", StaticFiles(directory=APP_DIR), name="static")
