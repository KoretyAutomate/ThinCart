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

import db

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
    type: Literal["add", "checkoff", "remove", "undo_checkoff"]
    actor: str = Field("", max_length=40)
    # add
    name: str | None = Field(None, max_length=120)
    qty_note: str | None = Field(None, max_length=120)
    # add (client-generated item uuid) / checkoff / remove
    item_id: str | None = Field(None, min_length=8, max_length=64)
    # undo_checkoff: the op_id of the checkoff being undone
    target_op_id: str | None = Field(None, max_length=64)


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


APPLY = {
    "add": apply_add,
    "checkoff": apply_checkoff,
    "remove": apply_remove,
    "undo_checkoff": apply_undo_checkoff,
}


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
    return {"ok": True, "result": result, "revision": db.get_revision(conn)}


@app.get("/api/state")
async def get_state():
    return db.state(conn)


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
