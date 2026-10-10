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


def _reconcile(stale: list[dict], old: dict | None, fresh: dict, unasked: list[int]) -> tuple[list[dict], list[dict]]:
    """(winners, held). `stale` is every old quote whose store could not be
    asked this time — the old winners AND quotes held from earlier partial
    checks. Winners are the lowest-priced among those (old cost) and the fresh
    quotes of the stores that answered (fresh cost), when all are costs in one
    dimension; held is the rest of `stale`, kept until its store answers, so a
    store that stays unreachable is never forgotten. Otherwise the unasked old
    winners stand as they were."""
    if not old or old.get("dim") is None or old.get("dim") != fresh.get("dim"):
        return [w for w in stale if not w.get("held")], []
    pool = [w for w in stale if w.get("cost") is not None]
    if len(pool) < len(stale) or not pool:   # a pre-tie answer has no costs to compare
        return [w for w in stale if not w.get("held")], []
    pool += [q for q in fresh["quotes"] if q.get("cost") is not None and q["store_id"] not in unasked]
    low = min(w["cost"] for w in pool)
    win = [{k: v for k, v in w.items() if k != "held"} for w in pool if abs(w["cost"] - low) < 1e-9]
    ids = {w["store_id"] for w in win}
    return win, [{**w, "held": True} for w in stale if w["store_id"] not in ids]


async def _persist(items: dict[int, dict], result: dict[str, dict]) -> None:
    """Keep each cheapest answer. A stored answer survives a refresh only when
    its own winning store could not be asked — that store may still be the
    cheapest. When the winner's store DID answer (with something else, or with
    nothing), the stored answer is contradicted: the new comparison replaces
    it, or it is dropped. With no stored answer, a definitive "nothing gives a
    cheapest store" (every store answered) records nothing."""
    if _ctx is None:  # pragma: no cover — wiring error
        return
    conn = _db()
    changed = False
    async with _ctx.write_lock:
        ts = _ctx.now_iso()
        for cid, it in items.items():
            r = result[str(cid)]
            partial = "unasked" in r["stores"].values()
            old = price_reco.stored_answer(conn, cid, it["key"])
            # every store at the old lowest price: a tie has more than one winner
            old_rows = [old["cheapest"], *old.get("tied", [])] if old and old.get("cheapest") else []
            winners = [w["store_id"] for w in old_rows]
            winner = winners[0] if winners else None
            # a regular product stood in only because nobody had it organic;
            # an organic one now found replaces it, wherever the old one was
            # organic products found at all — ranked or not — end the stand-in
            organic_now = bool(old and old.get("organic_fallback") and r["quotes"] and not r["organic_fallback"])
            # Judge the stored winner by the search that can speak for it: a
            # regular-product stand-in by the regular search, an organic winner
            # by the organic one. Not asked there (unreachable, or that search
            # did not run) means not contradicted — keep it (Codex review).
            checked_by = r["regular_stores"] if (old and old.get("organic_fallback")) or not it["organic"] \
                else r["organic_stores"]
            not_asked = {w["store_id"] for w in old_rows + (old or {}).get("held", [])
                         if checked_by.get(str(w["store_id"]), "unasked") == "unasked"}
            unasked = [w for w in winners if w in not_asked]
            if unasked and not organic_now:
                # Winners that could not be asked keep their old quote; the
                # stores that answered are judged on their fresh quote, in the
                # same dimension, so a confirmed tie stays and a cheaper price
                # becomes the winner. A beaten unreachable quote is HELD, not
                # dropped, until its own store answers (Codex review).
                stale = [w for w in old_rows + (old or {}).get("held", []) if w["store_id"] in not_asked]
                kept, held = _reconcile(stale, old, r, sorted(not_asked))
                if old and (kept != old_rows or held != old.get("held", [])):
                    # the statuses are THIS check's: the new winner was found by a partial one
                    fresh = {k: r[k] for k in ("stores", "organic_stores", "regular_stores")}
                    changed |= price_reco.save(conn, cid, it["key"], {
                        **old, **fresh, "cheapest": kept[0], "tied": kept[1:], "held": held}, ts)
                continue
            if r["cheapest"] and old and not organic_now and not_asked:
                # no old winner is unreachable, but a HELD quote still is: it competes
                kept, held = _reconcile([w for w in old.get("held", []) if w["store_id"] in not_asked],
                                        old, r, sorted(not_asked))
                if kept:
                    changed |= price_reco.save(conn, cid, it["key"],
                                               {**r, "cheapest": kept[0], "tied": kept[1:], "held": held}, ts)
                    continue
            if r["cheapest"]:
                changed |= price_reco.save(conn, cid, it["key"], r, ts)
            elif not partial or winner is not None:
                price_reco.forget(conn, cid, it["key"])   # definitive, or the winner contradicted
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
    # each pass keeps its own statuses: one store can answer the organic search
    # and be unreachable for the regular one
    first_pass = {cid: dict(o["stores"]) for cid, o in out.items()}
    regular_pass: dict[int, dict] = {}
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
            regular_pass[cid] = dict(o["stores"])
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
            # per-pass statuses, so a stored answer is judged by its own search
            "organic_stores": first_pass[cid] if it["organic"] else {},
            "regular_stores": first_pass[cid] if not it["organic"] else regular_pass.get(cid, {}),
            "buy_qty": it["buy_qty"], "buy_qty_ok": not it["buy_qty"] or it["wanted"] is not None,
            "reason": None if cmp["quotes"] else _reason(set(o["stores"].values())),
        }
    await _persist(items, result)
    return {"stores": [{"id": s["id"], "name": s["name"]} for s in stores],
            "items": result, "partial": partial, "organic": db.organic_setting(conn),
            "limit": PRICE_LIMIT}
