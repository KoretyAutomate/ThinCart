"""
where_api.py — POST /api/where: the cheapest store for each item (PLAN.md Phase 8).

Split out of lookup_api.py, which stays the store/product/aisle surface. This
is the price question: for each item, every store's fitting candidates
(in stock, priced, organic if the item is food and the household buys organic,
the preferred brand, and relevant — rice is not rice cakes), compared in ONE
unit we compute ourselves (quantity.py), by what it costs to buy the amount
the owner wants — or per unit when they have not said.

The answer is persisted (price_reco.py) so the list's 🏬 chips and the plan
show it on both phones; it is a recommendation, never a saved preference —
the owner's own pick (preferred_store_id) always wins over it.
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db
import price_reco
import quantity
import where
from chain_lookup import PRICE_LIMIT, price_products_many
from lookup import TTL, UNAVAILABLE
from lookup_api import _db, _pick_for, _require

router = APIRouter()

WHERE_MAX_ITEMS = 30  # per request: the phone pages through a longer list


@dataclass
class Context:
    write_lock: asyncio.Lock
    broadcast: Callable[[], Awaitable[None]]
    now_iso: Callable[[], str]


_ctx: Context | None = None


def bind(ctx: Context) -> None:
    global _ctx
    _ctx = ctx


class WhereRequest(BaseModel):
    catalog_ids: list[int] = Field(..., max_length=WHERE_MAX_ITEMS)


def _items(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    items = {}
    for cid in dict.fromkeys(ids):
        r = conn.execute(
            "SELECT id, display_name, aliases_json, brand, buy_qty FROM item_catalog WHERE id=?", (cid,)
        ).fetchone()
        if r is None:
            continue
        name = db.name_en(r["aliases_json"], r["display_name"]) or r["display_name"]
        # The preferred brand goes INTO the search: only the first matches come
        # back, and a brand ranked below them for the bare name would read as
        # "not sold here". Each result is still checked for it.
        brand = (r["brand"] or "").strip()
        plain = name if not brand or where.brand_ok({"name": name}, brand) else f"{brand} {name}"
        organic_term = plain if where.is_organic({"name": plain}) else (
            f"{brand} organic {name}" if plain != name else f"organic {name}")
        items[cid] = {
            "name": name, "plain": plain, "organic_term": organic_term, "brand": brand,
            "organic": price_reco.organic_applies(conn, cid),
            "buy_qty": r["buy_qty"] or "", "wanted": quantity.parse_wanted(r["buy_qty"] or ""),
            "key": price_reco.input_key(conn, cid),
        }
    return items


async def _ask_store(store: dict, items: dict[int, dict], organic_pass: bool, out: dict[int, dict]) -> bool:
    """One batch for one store. Records each item's status and candidates.
    False when the store could not be asked about at least one item."""
    terms: dict[int, str] = {}
    picks: dict[int, str | None] = {}
    for cid, it in items.items():
        pick = _pick_for(cid, store["chain"])
        organic = organic_pass and it["organic"]
        terms[cid] = pick["name"] if pick else (it["organic_term"] if organic else it["plain"])
        picks[cid] = pick["sku"] if pick else None
    found, complete = await price_products_many(
        store["chain"], list(terms.values()), store["chain_store_id"], max_age=TTL["price"])
    for cid, term in terms.items():
        if term not in found:
            out[cid]["stores"][str(store["id"])] = "unasked"
            continue
        organic = organic_pass and items[cid]["organic"]
        cands, status = where.fitting(found[term], picks[cid], organic, items[cid]["brand"], items[cid]["name"])
        out[cid]["stores"][str(store["id"])] = status
        out[cid]["cands"] += [(store, rec, exact) for rec, exact in cands]
    return complete


async def _ask_all(stores: list[dict], items: dict[int, dict], organic_pass: bool) -> tuple[dict, bool]:
    out: dict[int, dict] = {cid: {"cands": [], "stores": {}} for cid in items}
    partial = False
    for store in stores:
        if items and not await _ask_store(store, items, organic_pass, out):
            partial = True
    return out, partial


def _reason(statuses: set[str]) -> str:
    # "nothing here fits" is only claimed when every store answered
    if "unasked" in statuses:
        return "unasked"
    return "conflict" if "conflict" in statuses else "pick_missing" if "pick_missing" in statuses else "no_match"


async def _persist(items: dict[int, dict], result: dict[str, dict]) -> None:
    """Keep each cheapest answer. Without one, drop the stored answer when every
    store DID answer — nothing fits, the picked product is gone or out of
    stock, or what was found cannot be compared: the old recommendation is now
    wrong. Only a failure to ask keeps the last answer (it carries its age)."""
    if _ctx is None:  # pragma: no cover — wiring error
        return
    conn = _db()
    changed = False
    async with _ctx.write_lock:
        ts = _ctx.now_iso()
        for cid, it in items.items():
            r = result[str(cid)]
            partial = "unasked" in r["stores"].values()
            if r["cheapest"] and partial and price_reco.stored_for(conn, cid, it["key"]):
                # A store that could not be asked may still be the cheapest:
                # an incomplete comparison does not replace a complete one.
                continue
            if r["cheapest"]:
                changed |= price_reco.save(conn, cid, it["key"], r, ts)
            elif "unasked" not in r["stores"].values():
                price_reco.forget(conn, cid, it["key"])
                changed = True
        if changed:
            db.bump_revision(conn)
        conn.commit()
    if changed:
        await _ctx.broadcast()


@router.post("/api/where")
async def where_to_buy(req: WhereRequest) -> dict:
    """Cheapest priced store per item, among each store's first PRICE_LIMIT
    matches. With the household's organic setting on, FOOD is searched organic;
    an item no store carries organic — every store answered, none had one —
    is asked again for the regular product (`organic_fallback`). A store is
    named cheapest only when candidates can be measured in one unit."""
    _require("price")
    conn = _db()
    stores = price_reco.priced_stores(conn)
    items = _items(conn, req.catalog_ids)
    out, partial = await _ask_all(stores, items, True)
    if partial and items and all(
        not o["cands"] and all(s == "unasked" for s in o["stores"].values()) for o in out.values()
    ):
        raise HTTPException(503, {"code": UNAVAILABLE})
    retry = {cid: it for cid, it in items.items()
             if it["organic"] and not out[cid]["cands"] and set(out[cid]["stores"].values()) <= {"no_match"}}
    fallback: set[int] = set()
    if retry:
        again, more = await _ask_all(stores, retry, False)
        partial = partial or more
        for cid, o in again.items():
            if o["cands"]:
                out[cid] = o
                fallback.add(cid)
            else:  # could not be asked the second time: unasked, not "has nothing"
                out[cid]["stores"].update({k: v for k, v in o["stores"].items() if v == "unasked"})
    result = {}
    for cid, o in out.items():
        it = items[cid]
        cmp = where.compare(o["cands"], it["wanted"], quantity.is_liquid(it["name"]))
        result[str(cid)] = {
            **cmp, "stores": o["stores"], "brand": it["brand"], "organic_fallback": cid in fallback,
            "buy_qty": it["buy_qty"], "buy_qty_ok": not it["buy_qty"] or it["wanted"] is not None,
            "reason": None if cmp["quotes"] else _reason(set(o["stores"].values())),
        }
    await _persist(items, result)
    return {"stores": [{"id": s["id"], "name": s["name"]} for s in stores],
            "items": result, "partial": partial, "organic": db.organic_setting(conn),
            "limit": PRICE_LIMIT}
