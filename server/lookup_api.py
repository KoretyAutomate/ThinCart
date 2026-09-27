"""
lookup_api.py — the HTTP surface of the outbound lookups.

Split from lookup.py when it crossed the repo's 600-line ceiling. The seam is
the one that matters: **lookup.py remains the only module in this repo that
makes an outbound request.** Nothing here calls httpx; these handlers ask
lookup.py for facts and shape them into responses. That invariant is the whole
reason the outbound surface is auditable by reading one file, so a request made
from this module would be a review failure, not a style nit.

What does live here is the DB side of answering — which stores can be priced at
all, which product the household picked — and the rules about how an answer is
allowed to be presented. Those rules are the load-bearing part:

  - a near match leaves the price comparison rather than being marked inside it,
  - "nobody could ask" is reported apart from "there is no aisle",
  - a total outage is a 503, never a 200 with an empty list.
"""

import sqlite3

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import lookup
from lookup import (
    DISABLED,
    TTL,
    UNAVAILABLE,
    _nominatim,
    cache_get,
    cache_put,
    enabled,
    parse_store_results,
)
from chain_lookup import products, products_many
from branches import resolve_branch, store_from_link
from chains import aisle_label, detect
import db
import where

router = APIRouter()


def _require(feature: str) -> None:
    if not enabled(feature):
        raise HTTPException(503, {"code": DISABLED, "mode": lookup.MODE})


def _db() -> sqlite3.Connection:
    """The connection lookup.bind() was handed at startup. It is Optional there,
    and an endpoint reading a None connection should 503 rather than crash."""
    if lookup._conn is None:
        raise HTTPException(503, {"code": UNAVAILABLE})
    return lookup._conn


def _store_row(store_id: int) -> dict | None:
    r = _db().execute(
        "SELECT id, name, brand, address, lat, lon, chain, chain_store_id FROM stores WHERE id=?", (store_id,)
    ).fetchone()
    return dict(r) if r else None


# The states Wegmans operates in. Used only to turn an OSM address into their
# own store-page slug; an address we cannot parse simply yields no link, and a
# store with no link has no prices, which is the ordinary case.
def _priced_stores() -> list[dict]:
    """Stores that can answer a price question at all. Usually a subset, often
    empty — the UI must say 'no priced stores' rather than 'no prices'."""
    return [
        dict(r)
        for r in _db().execute(
            "SELECT id, name, chain, chain_store_id FROM stores "
            "WHERE chain != '' AND chain_store_id != '' ORDER BY name"
        )
    ]


def _pick_for(catalog_id: int, chain: str) -> dict | None:
    r = _db().execute(
        "SELECT sku, name, brand, pack_size FROM product_picks WHERE catalog_id=? AND chain=?",
        (catalog_id, chain),
    ).fetchone()
    return dict(r) if r else None


def _best(recs: list[dict], sku: str | None) -> tuple[dict | None, bool]:
    """(record, exact). Exact means it IS the product the household picked.
    Otherwise it is the closest name match, and the UI says so — an approximate
    match is useful for finding an aisle and misleading as a price."""
    if sku:
        for r in recs:
            if r["sku"] == sku:
                return r, True
    return (recs[0], False) if recs else (None, False)


@router.get("/api/stores/link")
async def store_link(store_id: int) -> dict:
    """Resolve a pinned store to the chain's own branch number, so its prices
    and aisles can be asked for. Three chains have an adapter; every other
    store is answered honestly rather than approximately."""
    _require("stores")
    store = _store_row(store_id)
    if not store:
        raise HTTPException(404, "no such store")
    chain = detect(store["name"], store["brand"])
    if not chain:
        return {"chain": "", "chain_store_id": "", "reason": "no price adapter for this store"}
    # Reaching the chain is a content lookup, not a store search: THINCART_LOOKUP
    # =stores promises OpenStreetMap and nothing else, and linking prices would
    # quietly step outside that.
    if not enabled("price"):
        return {"chain": "", "chain_store_id": "",
                "reason": f"price lookups are off (THINCART_LOOKUP={lookup.MODE})"}
    found = await resolve_branch(chain, store)
    if not found["chain_store_id"]:
        return {"chain": "", "chain_store_id": "", "reason": found["reason"]}
    # For a store OSM has never heard of, the chain's own directory supplied
    # the address and coordinates; they ride along so the phone can pin the row
    # by them, and the store stops being a bare name.
    extra = {k: found[k] for k in ("address", "lat", "lon", "confirm") if found.get(k) is not None}
    return {"chain": chain, "chain_store_id": found["chain_store_id"], **extra}


@router.get("/api/stores/from_link")
async def stores_from_link(url: str = Query(..., min_length=8, max_length=800)) -> dict:
    """A pasted Google Maps pin or a chain's store page -> one store hit in the
    same shape the OpenStreetMap search returns, with the chain link already
    resolved where the chain could be reached. For the shop the map has never
    heard of. {"result": None, "reason"} when the link says nothing usable."""
    _require("stores")
    return await store_from_link(url, price_ok=enabled("price"))


@router.get("/api/products/search")
async def products_search(catalog_id: int, store_id: int) -> dict:
    """The brand/size options for one catalog item, at one store."""
    _require("product")
    store = _store_row(store_id)
    if not store or not store["chain_store_id"]:
        raise HTTPException(400, "that store has no price source")
    row = _db().execute("SELECT display_name FROM item_catalog WHERE id=?", (catalog_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such item")
    # Search for the REMEMBERED product where there is one. Searching the
    # generic name again can leave the chosen sku outside the top hits, so the
    # options come back without it — the pick is shown as made yet cannot be
    # seen or compared, which defeats remembering it at all.
    pick = _pick_for(catalog_id, store["chain"])
    term = (pick["name"] if pick else "") or row["display_name"]
    recs = await products(store["chain"], term, store["chain_store_id"],
                          prefer_sku=pick["sku"] if pick else None)
    if recs is None:
        raise HTTPException(503, {"code": UNAVAILABLE})
    # The label the aisle view would render for each option. Carrying it here
    # means choosing a product can update the aisle view directly, instead of
    # re-asking the server and racing the not-yet-committed pick.
    options = [dict(r, aisle_label=aisle_label(r)) for r in recs]
    return {
        "options": options, "store": store["name"], "chain": store["chain"],
        "picked": pick,
    }


@router.get("/api/prices")
async def prices(
    catalog_id: int,
    sku: str = Query("", max_length=40),
    name: str = Query("", max_length=200),
    chain: str = Query("", max_length=30),
) -> dict:
    """What the picked product costs at each store that can say.

    `sku` AND `name` let the caller name the product outright, because the
    phone's op queue is optimistic: a just-made pick has not necessarily reached
    the database when this is called. The sku alone is not enough — the search
    still has to FIND that product, and searching a store's catalogue for the
    generic "sunflower butter" may not return the exact jar in its top few hits,
    leaving an approximate quote where an exact one exists.

    `chain` says whose sku it is. A Wegmans sku, a Whole Foods ASIN and a
    ShopRite UPC are three namespaces; applied to every store, a just-picked
    ASIN could never match at ShopRite and its quote would be demoted to an
    alternative even where the household has already picked there. So the
    supplied product is used at its own chain, and every other store is asked
    about ITS remembered pick — or, before one, the item's own name.
    """
    _require("price")
    row = _db().execute("SELECT display_name FROM item_catalog WHERE id=?", (catalog_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such item")
    # Quotes are claims about ONE product. Once the household has said which jar
    # they buy, another jar's price is not a cheaper version of it — and putting
    # the two in one amount-sorted list implies a comparability that marking the
    # row does not undo. So a near match is moved out of the comparison and
    # offered beside it as what the shop has instead.
    quotes: list[dict] = []
    alternatives: list[dict] = []
    failed = False
    for store in _priced_stores():
        pick = _pick_for(catalog_id, store["chain"])
        mine = bool(sku) and (chain == store["chain"] or not chain)
        want = (sku if mine else "") or (pick["sku"] if pick else None)
        term = (name if mine else "") or (pick["name"] if pick else row["display_name"])
        # A price may be served from cache, but only a price-fresh one: the same
        # record is happily reused for months to answer "which aisle".
        recs = await products(store["chain"], term, store["chain_store_id"],
                              max_age=TTL["price"], prefer_sku=want)
        if recs is None:
            failed = True
            continue
        rec, exact = _best(recs, want)
        if rec is None or rec["amount"] is None:
            continue
        # Before a pick there is nothing to be approximate TO: the best match on
        # the item's own name is the answer, and the UI marks it as such.
        quote = {
                "store_id": store["id"], "store": store["name"], "product": rec["name"],
                "brand": rec["brand"], "pack_size": rec["pack_size"], "amount": rec["amount"],
                "unit_price": rec["unit_price"], "available": rec["available"],
                "aisle": aisle_label(rec), "exact": exact, "source": rec["source"],
                "source_url": rec.get("source_url", ""),
                # The record's OWN age, not this response's clock. Reporting the
                # latter would make a two-day-old price read as current.
                "fetched_at": rec.get("fetched_at", ""),
        }
        (alternatives if (want and not exact) else quotes).append(quote)
    # Nothing came back AND something broke: that is an outage, not an absence.
    # Returning 200 with no quotes lets the UI's empty-check fire first and say
    # "no price found", which is a claim about the shop rather than about us.
    if failed and not quotes and not alternatives:
        raise HTTPException(503, {"code": UNAVAILABLE})
    quotes.sort(key=lambda q: q["amount"])
    alternatives.sort(key=lambda q: q["amount"])
    return {"quotes": quotes, "alternatives": alternatives, "partial": failed}


@router.get("/api/aisles")
async def aisles(store_id: int) -> dict:
    """Where each thing on the list lives in THIS store."""
    _require("aisle")
    store = _store_row(store_id)
    if not store or not store["chain_store_id"]:
        raise HTTPException(400, "that store has no aisle data")
    rows = _db().execute(
        "SELECT i.catalog_id, c.display_name FROM items i "
        "JOIN item_catalog c ON c.id = i.catalog_id"
    ).fetchall()
    picks = {r["catalog_id"]: _pick_for(r["catalog_id"], store["chain"]) for r in rows}
    terms = {}
    for r in rows:
        p = picks.get(r["catalog_id"])
        terms[r["catalog_id"]] = p["name"] if p else r["display_name"]
    # EVERY pick under a term, not the last one written: two items can share a
    # search term and mean different jars, and each has to find its shelf.
    prefer: dict[str, list[str]] = {}
    for cid, p in picks.items():
        if p:
            prefer.setdefault(terms[cid], []).append(p["sku"])
    got, complete = await products_many(
        store["chain"], list(dict.fromkeys(terms.values())), store["chain_store_id"], prefer
    )
    if not got and not complete:
        raise HTTPException(503, {"code": UNAVAILABLE})
    # WHICH items went unasked, not merely that some did. A global flag leaves
    # the UI unable to tell them from items that genuinely have no aisle, so it
    # shows them as "unknown" anyway — the exact confusion the flag was added
    # to prevent.
    unasked = [str(cid) for cid, term in terms.items() if term not in got]
    out = {}
    for cid, term in terms.items():
        pick = picks.get(cid)
        rec, exact = _best(got.get(term, []), pick["sku"] if pick else None)
        if rec is None:
            continue
        label = aisle_label(rec)
        if label:
            out[str(cid)] = {"label": label, "aisle": rec["aisle"], "exact": exact}
    # `partial` is the difference between "this has no aisle" and "nobody could
    # ask about this one". The UI must not render the second as the first.
    return {"aisles": out, "store": store["name"], "partial": not complete,
            "unasked": unasked}


@router.get("/api/stores/search")
async def stores_search(
    q: str = Query(..., min_length=1, max_length=80),
    area: str = Query("", max_length=80),
) -> dict:
    """Candidate real-world shops for the name the user typed.

    Returns an empty list rather than an error when nothing matches — 'no such
    shop near there' is a real answer the UI shows as itself, and the free-text
    'add anyway' path stays available for a shop OSM has never heard of.
    """
    _require("stores")
    key = f"{q.strip().lower()}|{area.strip().lower()}"
    cached = cache_get("store", key)
    if cached is not None:
        return {"results": cached, "source": "openstreetmap", "cached": True}

    raw = await _nominatim(
        {
            "q": f"{q.strip()} {area.strip()}".strip(),
            "format": "jsonv2",
            "limit": "12",
            "addressdetails": "1",
            "extratags": "1",
        }
    )
    if raw is None:
        raise HTTPException(503, {"code": UNAVAILABLE})
    results = parse_store_results(raw)
    cache_put("store", key, results)
    return {"results": results, "source": "openstreetmap", "cached": False}


# --- where to buy, by price (PLAN.md Phase 7C) ---------------------------------

WHERE_MAX_ITEMS = 30  # per request: the phone pages through a longer list


class WhereRequest(BaseModel):
    catalog_ids: list[int] = Field(..., max_length=WHERE_MAX_ITEMS)


def _where_items(ids: list[int]) -> dict[int, dict]:
    items = {}
    for cid in dict.fromkeys(ids):
        r = _db().execute(
            "SELECT id, display_name, aliases_json, brand FROM item_catalog WHERE id=?", (cid,)
        ).fetchone()
        if r is None:
            continue
        plain = db.name_en(r["aliases_json"], r["display_name"]) or r["display_name"]
        organic = plain if where.is_organic({"name": plain}) else f"organic {plain}"
        items[cid] = {"plain": plain, "organic": organic, "brand": r["brand"]}
    return items


def _quote(store: dict, rec: dict, exact: bool) -> dict:
    return {
        "store_id": store["id"], "store": store["name"], "product": rec["name"],
        "brand": rec["brand"], "pack_size": rec["pack_size"], "amount": rec["amount"],
        "unit_price": rec["unit_price"], "available": rec["available"], "exact": exact,
        "source": rec["source"], "source_url": rec.get("source_url", ""),
        "fetched_at": rec.get("fetched_at", ""),
    }


async def _ask_store(store: dict, items: dict[int, dict], out: dict[int, dict], organic: bool) -> bool:
    """One batch for one store; fills each item's per-store status and quote.
    False when the store could not be asked about at least one item."""
    terms: dict[int, str] = {}
    picks: dict[int, str | None] = {}
    prefer: dict[str, list[str]] = {}
    for cid, it in items.items():
        pick = _pick_for(cid, store["chain"])
        terms[cid] = pick["name"] if pick else it["organic" if organic else "plain"]
        picks[cid] = pick["sku"] if pick else None
        if pick:
            prefer.setdefault(terms[cid], []).append(pick["sku"])
    found, complete = await products_many(
        store["chain"], list(terms.values()), store["chain_store_id"], prefer,
        max_age=TTL["price"], place=False,
    )
    for cid, term in terms.items():
        if term not in found:
            out[cid]["stores"][str(store["id"])] = "unasked"
            continue
        rec, status, exact = where.choose(found[term], picks[cid], organic, items[cid]["brand"])
        out[cid]["stores"][str(store["id"])] = status
        if rec is not None:
            out[cid]["quotes"].append(_quote(store, rec, exact))
    return complete


async def _ask_all(stores: list[dict], items: dict[int, dict], organic: bool) -> tuple[dict[int, dict], bool]:
    out: dict[int, dict] = {cid: {"quotes": [], "stores": {}} for cid in items}
    partial = False
    for store in stores:
        if items and not await _ask_store(store, items, out, organic):
            partial = True
    return out, partial


@router.post("/api/where")
async def where_to_buy(req: WhereRequest) -> dict:
    """Cheapest priced store for each item, honouring the household's organic
    setting and each item's preferred brand.

    A VIEW, never a saved preference: nothing here writes preferred_store_id.
    Prices are fresh within TTL["price"], asked without shelf placement, one
    batch per store. A store is named cheapest only on comparable unit prices
    (where.rank); every quote carries its own source and age.

    With organic on, an item that no store carries organic — every store
    answered, none had one — is asked again for the regular product, and says
    so (`organic_fallback`), rather than showing nothing: some things (paper
    towels) do not come organic.
    """
    _require("price")
    stores = _priced_stores()
    items = _where_items(req.catalog_ids)
    organic = db.organic_setting(_db())
    out, partial = await _ask_all(stores, items, organic)
    # Nothing answered at all: an outage, not an absence. Judged on this first
    # pass — a failed fallback after answered organic searches is not one.
    if partial and items and not any(o["quotes"] for o in out.values()) and all(
        s == "unasked" for o in out.values() for s in o["stores"].values()
    ):
        raise HTTPException(503, {"code": UNAVAILABLE})
    fallback: set[int] = set()
    if organic:
        # Only a genuine "no store has it organic": every store answered
        # no_match. A saved conventional pick is a CONFLICT with the setting —
        # retrying without it would quietly price the pick as a fallback, even
        # where the store had an organic one.
        retry = {cid: it for cid, it in items.items()
                 if not out[cid]["quotes"] and set(out[cid]["stores"].values()) <= {"no_match"}}
        if retry:
            again, more = await _ask_all(stores, retry, False)
            partial = partial or more
            for cid, o in again.items():
                if o["quotes"]:
                    out[cid] = o
                    fallback.add(cid)
                else:
                    # a store that could not be asked the second time is
                    # unasked, not "has nothing" — keep that honest
                    for sid, st in o["stores"].items():
                        if st == "unasked":
                            out[cid]["stores"][sid] = "unasked"
    result = {}
    for cid, o in out.items():
        o["quotes"].sort(key=lambda q: q["amount"])
        cheapest, comparable = where.rank(o["quotes"])
        statuses = set(o["stores"].values())
        # "nothing here fits" is only claimed when every store answered
        reason = None
        if not o["quotes"]:
            reason = "unasked" if "unasked" in statuses else (
                "conflict" if "conflict" in statuses else
                "pick_missing" if "pick_missing" in statuses else "no_match")
        result[str(cid)] = {
            "cheapest": cheapest, "comparable": comparable, "quotes": o["quotes"],
            "stores": o["stores"], "reason": reason, "brand": items[cid]["brand"],
            "organic_fallback": cid in fallback,
        }
    return {
        "stores": [{"id": s["id"], "name": s["name"]} for s in stores],
        "items": result,
        "partial": partial,
        "organic": organic,
    }
