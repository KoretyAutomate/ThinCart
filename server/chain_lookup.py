"""
chain_lookup.py — which chain answers how, and where a product sits on the shelf.

Split out of lookup.py (which stays the one file that sends anything outward):
this module decides WHAT to ask and in which order, and every request it causes
is made by a lookup.* function. Internals are reached as `lookup.<name>` so the
tests' monkeypatching of lookup keeps working.
"""

import asyncio
from datetime import timedelta

import lookup


async def _place(chain: str, store: str, recs: list[dict], prefer: list[str]) -> bool:
    """Fill the shelf position on the top hit and on every product the household
    picked that is among the hits — `prefer` is a list because two items can
    share a search term and mean different jars. Both chains keep the position
    on the product record, so placing every hit would cost a request each;
    these few cost a few, cached for the aisle TTL. "Asked, has no place" is
    cached as an empty dict; a failed read is not cached, so it is asked again
    — an answer about the shop versus a failure to ask. True when a record
    changed."""
    targets = [r for r in recs[:1] if r["sku"]]
    targets += [r for r in recs if r["sku"] in prefer and r not in targets]
    changed = False
    for rec in targets:
        if rec.get("aisle"):
            continue
        key = f"{chain}:{store}|{rec['sku']}"
        loc = lookup.cache_get("aisle", key)
        if loc is None:
            loc = await lookup._LOCATE[chain](store, rec["sku"])
            if loc is None:
                continue
            lookup.cache_put("aisle", key, loc)
        new = (loc.get("aisle", ""), loc.get("shelf", ""))
        if new != (rec.get("aisle", ""), rec.get("shelf", "")):
            rec["aisle"], rec["shelf"] = new
            changed = True
    return changed


async def _chain_search(chain: str, term: str, store: str, limit: int,
                        max_age: timedelta | None) -> list[dict] | None:
    """The search half, cached. A price question stops here: shelf placement
    costs a request per product, which across a whole list is most of the
    traffic, and the price does not need it."""
    key = f"{chain}:{store}|{term.strip().lower()}|{limit}"
    recs = lookup.cache_get("product", key, max_age)
    if recs is None:
        fetched = await (lookup._wf_search(term, store) if chain == "wholefoods"
                         else lookup._sr_search(term, store, limit))
        if fetched is None:
            return None
        recs = fetched[:limit]
        lookup.cache_put("product", key, recs)
    return recs


async def _chain_products(chain: str, term: str, store: str, limit: int,
                          max_age: timedelta | None, prefer: list[str]) -> list[dict] | None:
    """Search-then-place. The search is cached once, when fetched, and never
    written back: positions live in their own cache and are laid on at every
    read, which costs nothing — writing placed records back would re-stamp the
    row, and a fortnight-old price would pass the two-day check as fresh."""
    recs = await _chain_search(chain, term, store, limit, max_age)
    if recs is None:
        return None
    await _place(chain, store, recs, prefer)
    return recs


# --- dispatch: the one place that knows which chain answers how ----------------


async def products(chain: str, term: str, store: str, limit: int = 8, max_age: timedelta | None = None,
                   prefer_sku: str | list[str] | None = None) -> list[dict] | None:
    """Products matching `term` at ONE store, in our shape. None = the lookup
    failed; [] = it genuinely found nothing. `prefer_sku` names the product(s)
    the household picked, so their shelf positions are fetched even when they
    are not the top hit."""
    if chain == "wegmans":
        return await lookup.wegmans_products(term, store, limit, max_age)
    if chain in lookup._LOCATE:
        prefer = [prefer_sku] if isinstance(prefer_sku, str) else list(prefer_sku or [])
        return await _chain_products(chain, term, store, limit, max_age, [s for s in prefer if s])
    return None


async def products_many(chain: str, terms: list[str], store: str,
                        prefer: dict[str, list[str]] | None = None,
                        max_age: timedelta | None = None,
                        place: bool = True) -> tuple[dict[str, list[dict]], bool]:
    """({term: records}, complete) — see wegmans_products_many for why
    `complete` exists. The two page-backed chains have no multi-query, so this
    asks term by term, a few at a time, and reports any it could not ask.
    `prefer` maps a term to EVERY sku picked under it: two items can share a
    search term and mean different jars, and each must find its shelf.
    `max_age` narrows the cache (a price must be fresher than an aisle);
    `place=False` skips shelf lookups for a price-only question."""
    if chain == "wegmans":
        return await lookup.wegmans_products_many(terms, store, max_age)
    if chain not in lookup._LOCATE:
        return {}, False
    sem = asyncio.Semaphore(3)

    async def one(t: str) -> tuple[str, list[dict] | None]:
        async with sem:
            if not place:
                return t, await _chain_search(chain, t, store, 5, max_age)
            return t, await products(chain, t, store, limit=5, max_age=max_age, prefer_sku=(prefer or {}).get(t))

    found: dict[str, list[dict]] = {}
    complete = True
    for t, recs in await asyncio.gather(*(one(t) for t in dict.fromkeys(terms))):
        if recs is None:
            complete = False
        else:
            found[t] = recs
    return found, complete


PRICE_LIMIT = 10  # a price question looks at each store's first 10 matches (PLAN.md Phase 8, delta 4)


async def price_products_many(chain: str, terms: list[str], store: str,
                              max_age: timedelta | None = None) -> tuple[dict[str, list[dict]], bool]:
    """({term: records}, complete) for a PRICE question: the first PRICE_LIMIT
    matches per term, no shelf placement (the price does not need the shelf,
    and placing costs a request per product). Same cache-first rules as
    products_many; a price must be fresher than an aisle, hence `max_age`."""
    if chain == "wegmans":
        return await lookup.wegmans_products_many(terms, store, max_age, PRICE_LIMIT)
    if chain not in lookup._LOCATE:
        return {}, False
    sem = asyncio.Semaphore(3)

    async def one(t: str) -> tuple[str, list[dict] | None]:
        async with sem:
            return t, await _chain_search(chain, t, store, PRICE_LIMIT, max_age)

    found: dict[str, list[dict]] = {}
    complete = True
    for t, recs in await asyncio.gather(*(one(t) for t in dict.fromkeys(terms))):
        if recs is None:
            complete = False
        else:
            found[t] = recs
    return found, complete
