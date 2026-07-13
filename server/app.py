"""
app.py — PlantCart multi-tenant server: accounts + households, per-household
shopping lists with real-time sync, purchase-cycle recommendations, plant
tracking, and pluggable LLM recipes.

Every data path is scoped to the caller's household (resolved from a JWT). The
WebSocket fans out per-household rooms; catalog enrichment (a shared-corpus
change) fans out to all rooms. See PLAN-saas.md.

Run:  uvicorn app:app --host 0.0.0.0 --port 8123
"""
import asyncio
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

import auth
import catalog
import config
import cycles
import db
import llm
import plants

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("plantcart")

APP_DIR = Path(__file__).parent.parent / "app"

app = FastAPI(title="PlantCart", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

conn = db.connect()
write_lock = asyncio.Lock()               # serializes all writes on the single connection
rooms: dict[str, set[WebSocket]] = {}     # household_id -> live sockets
require = auth.ctx_from_header(conn)       # FastAPI dependency: Bearer JWT -> Ctx


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================ security headers ============================

@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ============================ auth rate limiting ============================

_auth_hits: dict[str, list[float]] = {}
AUTH_WINDOW_S, AUTH_MAX = 300, 20  # 20 attempts / 5 min / IP on /api/auth/*


def _rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    hits = [t for t in _auth_hits.get(ip, []) if now - t < AUTH_WINDOW_S]
    if len(hits) >= AUTH_MAX:
        raise HTTPException(429, "too many attempts, slow down")
    hits.append(now)
    _auth_hits[ip] = hits


# ============================ auth endpoints ============================

class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field("", max_length=40)
    household_name: str = Field("", max_length=60)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class JoinBody(BaseModel):
    invite_code: str = Field(..., min_length=4, max_length=12)


@app.post("/api/auth/register")
async def register(body: RegisterBody, request: Request):
    _rate_limit(request)
    async with write_lock:
        uid = auth.create_user(conn, body.email, body.password, body.display_name)
        hh_name = body.household_name.strip() or f"{body.display_name or 'My'}'s list"
        hid = auth.create_household(conn, hh_name, uid)
        conn.commit()
    return {"token": auth.make_token(uid, hid), "household": auth.household_summary(conn, hid),
            "user_id": uid}


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    _rate_limit(request)
    row = conn.execute(
        "SELECT id, pw_hash FROM users WHERE email=?", (body.email.strip().lower(),)
    ).fetchone()
    if not row or not auth.verify_password(body.password, row["pw_hash"]):
        raise HTTPException(401, "invalid email or password")
    hh = conn.execute(
        "SELECT household_id FROM household_members WHERE user_id=? ORDER BY joined_at LIMIT 1",
        (row["id"],),
    ).fetchone()
    if not hh:  # user with no household (post-leave) — give them a fresh one
        async with write_lock:
            hid = auth.create_household(conn, "My list", row["id"])
            conn.commit()
    else:
        hid = hh["household_id"]
    return {"token": auth.make_token(row["id"], hid),
            "household": auth.household_summary(conn, hid), "user_id": row["id"]}


@app.post("/api/households/join")
async def join(body: JoinBody, ctx: auth.Ctx = Depends(lambda: None), authorization: str = Header("")):
    # join requires being logged in; resolve the user from the token but ignore old household
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    data = auth.decode_token(authorization[7:].strip())
    uid = data.get("sub")
    if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        raise HTTPException(401, "account no longer exists")
    async with write_lock:
        hid = auth.join_household(conn, body.invite_code, uid)
        conn.commit()
    return {"token": auth.make_token(uid, hid), "household": auth.household_summary(conn, hid),
            "user_id": uid}


@app.get("/api/households/me")
async def household_me(ctx: auth.Ctx = Depends(require)):
    return {"household": auth.household_summary(conn, ctx.household_id), "user_id": ctx.user_id}


@app.post("/api/account/delete", status_code=204)
async def delete_account(ctx: auth.Ctx = Depends(require)):
    """GDPR + Apple 5.1.1(v): remove the user; cascade any household they solely own."""
    async with write_lock:
        db.delete_user(conn, ctx.user_id)
    return Response(status_code=204)


# ============================ WebSocket ticket ============================

_ws_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (household_id, expiry)
WS_TICKET_TTL_S = 60


@app.post("/api/ws-ticket")
async def ws_ticket(ctx: auth.Ctx = Depends(require)):
    """Short-lived single-use ticket for the WS handshake — keeps the 30-day JWT
    out of the ?query= string (and out of proxy access logs)."""
    ticket = secrets.token_urlsafe(24)
    _ws_tickets[ticket] = (ctx.household_id, time.monotonic() + WS_TICKET_TTL_S)
    return {"ticket": ticket}


def _redeem_ticket(ticket: str) -> str | None:
    entry = _ws_tickets.pop(ticket, None)  # single-use
    if not entry:
        return None
    hid, exp = entry
    return hid if time.monotonic() < exp else None


# ============================ ops (household-scoped) ============================

class Op(BaseModel):
    op_id: str = Field(..., min_length=8, max_length=64)
    type: Literal["add", "checkoff", "remove", "skip", "undo_checkoff",
                  "undo_purchase", "snooze", "edit"]
    actor: str = Field("", max_length=40)
    # add / edit
    name: str | None = Field(None, max_length=120)
    qty_note: str | None = Field(None, max_length=120)
    # edit (category adjustment — one of catalog.CATEGORIES)
    category: str | None = Field(None, max_length=20)
    # add (client-generated item uuid) / checkoff / remove
    item_id: str | None = Field(None, min_length=8, max_length=64)
    target_op_id: str | None = Field(None, max_length=64)
    # undo_purchase: the purchase_events.id being corrected from the History panel
    event_id: int | None = None
    # snooze (suggestion dismissal — server-side so it silences BOTH phones)
    catalog_id: int | None = None


async def broadcast_state(hid: str) -> None:
    """Push full state to every socket in one household's room."""
    if hid not in rooms:
        return
    payload = json.dumps(db.state(conn, hid), ensure_ascii=False)
    for ws in list(rooms[hid]):
        try:
            await ws.send_text(payload)
        except Exception:
            rooms[hid].discard(ws)


async def broadcast_all() -> None:
    """Catalog enrichment changed the SHARED corpus (category/aliases/plants) →
    refresh every household's view (each gets its own scoped state)."""
    for hid in list(rooms.keys()):
        await broadcast_state(hid)


def apply_add(op: Op, ts: str, hid: str) -> dict:
    if not op.name or not op.name.strip():
        raise HTTPException(422, "add requires a non-empty name")
    catalog_id = db.get_or_create_catalog(conn, op.name)
    existing = conn.execute(
        "SELECT id FROM items WHERE catalog_id=? AND household_id=?", (catalog_id, hid)
    ).fetchone()
    if existing:  # already on THIS household's list → converge
        return {"item_id": existing["id"], "deduped": True}
    item_id = op.item_id or str(uuid.uuid4())
    rev = db.bump_revision(conn, hid)
    conn.execute(
        "INSERT INTO items(id, household_id, catalog_id, qty_note, added_by, added_at, revision) "
        "VALUES(?,?,?,?,?,?,?)",
        (item_id, hid, catalog_id, op.qty_note or "", op.actor, ts, rev),
    )
    return {"item_id": item_id, "catalog_id": catalog_id}


def apply_checkoff(op: Op, ts: str, hid: str) -> dict:
    if op.item_id is None:
        raise HTTPException(422, "checkoff requires item_id")
    row = conn.execute(
        "SELECT id, catalog_id, qty_note, added_by, added_at FROM items "
        "WHERE id=? AND household_id=?",
        (op.item_id, hid),
    ).fetchone()
    if row is None:
        return {"noop": True}
    conn.execute("DELETE FROM items WHERE id=? AND household_id=?", (op.item_id, hid))
    cur = conn.execute(
        "INSERT INTO purchase_events(household_id, catalog_id, bought_at, bought_by) "
        "VALUES(?,?,?,?)",
        (hid, row["catalog_id"], ts, op.actor),
    )
    db.bump_revision(conn, hid)
    return {
        "event_id": cur.lastrowid,
        "item": {k: row[k] for k in ("catalog_id", "qty_note", "added_by", "added_at")},
    }


def apply_remove(op: Op, ts: str, hid: str) -> dict:
    if op.item_id is None:
        raise HTTPException(422, "remove requires item_id")
    cur = conn.execute("DELETE FROM items WHERE id=? AND household_id=?", (op.item_id, hid))
    if cur.rowcount == 0:
        return {"noop": True}
    db.bump_revision(conn, hid)
    return {"removed": op.item_id}


def apply_skip(op: Op, ts: str, hid: str) -> dict:
    """Out of stock: off the list, NO purchase event, 1-day snooze (re-suggest next trip)."""
    if op.item_id is None:
        raise HTTPException(422, "skip requires item_id")
    row = conn.execute(
        "SELECT catalog_id FROM items WHERE id=? AND household_id=?", (op.item_id, hid)
    ).fetchone()
    if row is None:
        return {"noop": True}
    conn.execute("DELETE FROM items WHERE id=? AND household_id=?", (op.item_id, hid))
    until = (datetime.fromisoformat(ts) + timedelta(days=1)).isoformat(timespec="seconds")
    db.set_snooze(conn, hid, row["catalog_id"], until)
    db.bump_revision(conn, hid)
    return {"skipped": op.item_id, "resuggest_after": until}


def apply_undo_checkoff(op: Op, ts: str, hid: str) -> dict:
    if not op.target_op_id:
        raise HTTPException(422, "undo_checkoff requires target_op_id")
    target = db.get_applied(conn, hid, op.target_op_id)  # scoped: can't undo across households
    if not target or "event_id" not in target:
        return {"noop": True}
    conn.execute("DELETE FROM purchase_events WHERE id=? AND household_id=?",
                 (target["event_id"], hid))
    snap = target["item"]
    rev = db.bump_revision(conn, hid)
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items(id, household_id, catalog_id, qty_note, added_by, added_at, revision) "
        "VALUES(?,?,?,?,?,?,?)",
        (item_id, hid, snap["catalog_id"], snap["qty_note"], snap["added_by"], snap["added_at"], rev),
    )
    return {"item_id": item_id, "undone_event": target["event_id"]}


def apply_undo_purchase(op: Op, ts: str, hid: str) -> dict:
    """History-panel mis-swipe repair, keyed by the server event id so ANY past
    purchase can be corrected — not just one still inside the 7-day op ledger
    (undo_checkoff's limit). Deletes the purchase_event and puts the item back
    on the list so it isn't silently lost. Scoped: an event id belonging to
    another household is indistinguishable from a nonexistent one (no-op ACK)."""
    if op.event_id is None:
        raise HTTPException(422, "undo_purchase requires event_id")
    row = conn.execute(
        "SELECT catalog_id FROM purchase_events WHERE id=? AND household_id=?",
        (op.event_id, hid),
    ).fetchone()
    if row is None:  # already undone, double-tap, or not this household's event
        return {"noop": True}
    conn.execute("DELETE FROM purchase_events WHERE id=? AND household_id=?",
                 (op.event_id, hid))
    catalog_id = row["catalog_id"]
    rev = db.bump_revision(conn, hid)
    existing = conn.execute(
        "SELECT id FROM items WHERE catalog_id=? AND household_id=?", (catalog_id, hid)
    ).fetchone()
    if existing:  # already back on the list → the event deletion alone stands
        return {"undone_event": op.event_id, "item_id": existing["id"], "deduped": True}
    item_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO items(id, household_id, catalog_id, qty_note, added_by, added_at, revision) "
        "VALUES(?,?,?,?,?,?,?)",
        (item_id, hid, catalog_id, "", op.actor, ts, rev),
    )
    return {"undone_event": op.event_id, "item_id": item_id}


def apply_edit(op: Op, ts: str, hid: str) -> dict:
    """Long-press editor: adjust an item's quantity note and/or its category.
    Quantity is per-item (items.qty_note, household-scoped). Category must NOT
    write the SHARED item_catalog row — one household's aisle preference would
    silently re-categorize the item for every household — so it lands in the
    per-household catalog_category_override table, read-preferred over the
    global value (same pattern as catalog_snooze)."""
    if op.item_id is None:
        raise HTTPException(422, "edit requires item_id")
    row = conn.execute(
        "SELECT catalog_id FROM items WHERE id=? AND household_id=?",
        (op.item_id, hid),
    ).fetchone()
    if row is None:  # removed / bought by the other phone → nothing to edit
        return {"noop": True}
    changed = False
    if op.qty_note is not None:
        conn.execute("UPDATE items SET qty_note=? WHERE id=? AND household_id=?",
                     (op.qty_note, op.item_id, hid))
        changed = True
    if op.category is not None:
        if op.category not in catalog.CATEGORIES:
            raise HTTPException(422, "invalid category")
        conn.execute(
            "INSERT INTO catalog_category_override(household_id, catalog_id, category) "
            "VALUES(?,?,?) ON CONFLICT(household_id, catalog_id) "
            "DO UPDATE SET category=excluded.category",
            (hid, row["catalog_id"], op.category),
        )
        changed = True
    if changed:
        db.bump_revision(conn, hid)
    return {"edited": op.item_id, "changed": changed}


def apply_snooze(op: Op, ts: str, hid: str) -> dict:
    """Dismiss a suggestion for ½ its median cycle (min 2 days), scoped to household."""
    if op.catalog_id is None:
        raise HTTPException(422, "snooze requires catalog_id")
    hist = db.purchase_history(conn, hid).get(op.catalog_id, [])
    half = (cycles.median_interval_days(hist) or 7.0) / 2
    until = (datetime.fromisoformat(ts) + timedelta(days=max(half, 2.0))).isoformat(timespec="seconds")
    db.set_snooze(conn, hid, op.catalog_id, until)
    db.bump_revision(conn, hid)
    return {"snoozed_until": until}


APPLY = {
    "add": apply_add,
    "checkoff": apply_checkoff,
    "remove": apply_remove,
    "skip": apply_skip,
    "undo_checkoff": apply_undo_checkoff,
    "undo_purchase": apply_undo_purchase,
    "snooze": apply_snooze,
    "edit": apply_edit,
}


async def enrich_and_push(catalog_id: int) -> None:
    try:
        if await catalog.enrich(conn, write_lock, catalog_id):
            await broadcast_all()  # shared-corpus change → every room
    except Exception:
        log.exception("enrichment failed for catalog_id=%s", catalog_id)


async def enrich_sweeper() -> None:
    while True:
        try:
            if await catalog.sweep(conn, write_lock):
                await broadcast_all()
        except Exception:
            log.exception("enrichment sweep failed")
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def startup():
    asyncio.create_task(enrich_sweeper())


@app.post("/api/op")
async def post_op(op: Op, ctx: auth.Ctx = Depends(require)):
    hid = ctx.household_id
    async with write_lock:
        prior = db.get_applied(conn, hid, op.op_id)
        if prior is not None:
            return {"ok": True, "replayed": True, "result": prior,
                    "revision": db.get_revision(conn, hid)}
        ts = now_iso()
        result = APPLY[op.type](op, ts, hid)
        db.record_op(conn, hid, op.op_id, ts, result)
        db.prune_applied_ops(conn, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        conn.commit()
    await broadcast_state(hid)
    if op.type == "add" and "catalog_id" in result:
        row = conn.execute(
            "SELECT llm_enriched_at FROM item_catalog WHERE id=?", (result["catalog_id"],)
        ).fetchone()
        if row and row["llm_enriched_at"] is None:
            asyncio.create_task(enrich_and_push(result["catalog_id"]))
    return {"ok": True, "result": result, "revision": db.get_revision(conn, hid)}


@app.get("/api/state")
async def get_state(ctx: auth.Ctx = Depends(require)):
    return db.state(conn, ctx.household_id)


@app.get("/api/catalog")
async def get_catalog(ctx: auth.Ctx = Depends(require)):
    """Typing candidates. Frequency ranking is scoped to THIS household so the
    ordering never reveals other households' buying, and matches personal habit."""
    rows = conn.execute(
        """SELECT c.id, c.display_name AS name, c.category, c.aliases_json,
                  (SELECT COUNT(*) FROM purchase_events e
                   WHERE e.catalog_id=c.id AND e.household_id=?) AS buys
           FROM item_catalog c WHERE c.verified = 1
           ORDER BY buys DESC, c.display_name""",
        (ctx.household_id,),
    ).fetchall()
    return {"catalog": [
        {"name": r["name"], "category": r["category"],
         "name_en": db.name_en(r["aliases_json"], r["name"]),
         "aliases": json.loads(r["aliases_json"])}
        for r in rows
    ]}


@app.get("/api/cycles")
async def get_cycles(ctx: auth.Ctx = Depends(require)):
    hid = ctx.household_id
    now = datetime.now(timezone.utc)
    now_s = now.isoformat(timespec="seconds")
    on_list = {r["catalog_id"] for r in conn.execute(
        "SELECT catalog_id FROM items WHERE household_id=?", (hid,))}
    snoozed = db._snoozed_map(conn, hid, now_s)
    out = []
    for cid, ts in db.purchase_history(conn, hid).items():
        m = cycles.median_interval_days(ts)
        if not m:
            continue
        since = (now - cycles.coalesce(ts)[-1]).total_seconds() / 86400
        row = conn.execute(
            "SELECT display_name, aliases_json FROM item_catalog WHERE id=?", (cid,)
        ).fetchone()
        score = since / m
        out.append({
            "catalog_id": cid, "name": row["display_name"],
            "name_en": db.name_en(row["aliases_json"], row["display_name"]),
            "label": cycles.cycle_label(m), "median_days": round(m, 1),
            "days_since": round(since, 1), "score": round(score, 2),
            "due": (cycles.DUE_MIN <= score <= cycles.DUE_MAX
                    and cid not in on_list and cid not in snoozed),
            "on_list": cid in on_list,
        })
    out.sort(key=lambda x: -x["score"])
    return {"cycles": out}


@app.get("/api/history")
async def get_history(limit: int = 100, ctx: auth.Ctx = Depends(require)):
    """Recent purchases, newest first — the History panel that lets a mis-swipe
    be corrected (undo_purchase) long after the ~8 s undo toast is gone."""
    return {"history": db.recent_history(conn, ctx.household_id, min(limit, 500))}


IDEAS_TTL_H = 6
ideas_cache: dict[str, dict] = {}  # household_id -> {"data":..., "at":...}


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
async def get_ideas(refresh: int = 0, ctx: auth.Ctx = Depends(require)):
    """Recipes + diversity (LLM), cached PER HOUSEHOLD. Failure never blocks the list."""
    hid = ctx.household_id
    cached = ideas_cache.get(hid)
    if (not refresh and cached
            and (datetime.now(timezone.utc) - cached["at"]).total_seconds() < IDEAS_TTL_H * 3600):
        return cached["data"]

    available = [p["name"] for p in catalog.recent_purchases(conn, hid)]
    on_list = [i["name"] for i in db.state(conn, hid)["items"]]
    week = catalog.weekly_plants(conn, hid)
    month = catalog.weekly_plants(conn, hid, window_days=30)

    recipes, diversity = await asyncio.gather(
        llm.chat_json(_recipes_prompt(available, on_list, week), max_tokens=700, timeout=90),
        llm.chat_json(_diversity_prompt(month), max_tokens=400, timeout=90),
    )
    recipes = recipes.get("recipes") if isinstance(recipes, dict) else None
    diversity = diversity.get("suggestions") if isinstance(diversity, dict) else None
    if diversity is not None:
        # the LLM sometimes suggests plants just eaten despite the prompt —
        # enforce "different" deterministically against the 30-day set. Canonicalize
        # the suggestion first, or a synonym ("capsicum" for an eaten "bell pepper")
        # walks straight through the filter.
        eaten = set(month)
        diversity = [
            s for s in diversity
            if isinstance(s, dict) and s.get("buy")
            and not set(plants.normalize([str(s.get("plant", ""))])) & eaten
        ]
    if recipes is None and diversity is None:
        raise HTTPException(503, "LLM unavailable — list and sync are unaffected")

    data = {"recipes": recipes or [], "diversity": diversity or [], "generated_at": now_iso()}
    ideas_cache[hid] = {"data": data, "at": datetime.now(timezone.utc)}
    return data


@app.get("/health")
async def health():
    return {"ok": True, "llm": llm.available(), "households": len(rooms)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, ticket: str = "", token: str = ""):
    """Auth BEFORE joining a room. Prefer a short-lived ticket (?ticket=); a raw
    JWT (?token=) is accepted as a fallback for clients that can't fetch one."""
    await ws.accept()
    hid = _redeem_ticket(ticket) if ticket else None
    if hid is None and token:
        try:
            hid = auth.ctx_from_token(conn, token).household_id
        except Exception:
            hid = None
    if hid is None:
        await ws.close(code=1008)  # policy violation
        return
    rooms.setdefault(hid, set()).add(ws)
    try:
        await ws.send_text(json.dumps(db.state(conn, hid), ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        rooms.get(hid, set()).discard(ws)


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "index.html")


app.mount("/", StaticFiles(directory=APP_DIR), name="static")
