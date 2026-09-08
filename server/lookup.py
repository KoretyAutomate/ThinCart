"""
lookup.py — the ONE module in ThinCart allowed to make an outbound request.

Until Phase 6 this app made none at all: everything lived on the tailnet and the
only non-local dependency was vLLM on 127.0.0.1. Store search, price comparison
and aisle hints change that, and the household's shopping content is what
travels. Concentrating every outbound call here is what makes that reviewable —
an `httpx` or `curl_cffi` call anywhere else in this repo is a review failure,
not a style nit, and tests/test_chains.py greps for exactly that.

Three rules hold everywhere below, and the tests enforce them:

1. **Provenance or nothing.** Every record returned carries `source` and
   `fetched_at`. A fact that cannot say where it came from is not returned.
2. **Not-found is an answer.** When a lookup finds nothing it says so. It never
   falls back to a plausible number, and neither does any caller. A wrong aisle
   costs a lap of the shop; an invented price costs trust in every real one.
3. **Cache before network.** A repeat question is answered from SQLite. That is
   a privacy measure as much as a latency one — it bounds how often the shopping
   list is described to a search engine.

Best-effort throughout, exactly like ideas.py: the list and its sync never
depend on this module. With THINCART_LOOKUP=off every endpoint here 503s and
nothing else in the app notices.
"""

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from curl_cffi.requests import AsyncSession
from fastapi import HTTPException

import osm
import shoprite
import wholefoods
from wegmans import parse_store_number, parse_wegmans_hits

log = logging.getLogger("thincart.lookup")

# off = no outbound calls at all (the pre-Phase-6 behaviour, a supported state).
# stores = only the store registry may reach out; the query is a shop name and a
# town, and says nothing about what the household buys.
# all = store search, product search, prices and aisle hints.
# Defaults to OFF. The contract before Phase 6 was that this app made no
# outbound request at all, and that is what an unconfigured launch should still
# do — a dev run, a test, a clone of this public repo. Reaching outside is a
# thing a deployment opts into: the systemd unit sets THINCART_LOOKUP=all.
MODE = os.environ.get("THINCART_LOOKUP", "off").strip().lower()

# Overridable like the chain identifiers below: it is somebody else's endpoint.
# A default rather than a requirement, so the feature works on a fresh install —
# and a stale value fails visibly with "couldn't reach", never silently wrong.
NOMINATIM_URL = os.environ.get(
    "THINCART_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
).strip()
NOMINATIM_REVERSE_URL = os.environ.get(
    "THINCART_NOMINATIM_REVERSE_URL", "https://nominatim.openstreetmap.org/reverse"
).strip()
# Nominatim's usage policy requires an identifying User-Agent and at most one
# request a second. Both are conditions of being allowed to use it at all.
USER_AGENT = "ThinCart/1.0 (self-hosted household shopping list; +https://github.com/KoretyAutomate/ThinCart)"
NOMINATIM_MIN_INTERVAL = 1.1

TTL = {
    "store": timedelta(days=30),    # a shop's address effectively never moves
    "product": timedelta(days=14),
    "aisle": timedelta(days=60),    # layouts change on the order of months
    "price": timedelta(days=2),     # the one thing that genuinely moves
    "chain": timedelta(days=90),    # a chain's own store number for a branch
}

# --- Wegmans -----------------------------------------------------------------
# Their shop runs on Algolia. The `products` index holds one record per
# (product x store) — price AND shelf position together — and is scoped with
# filters="storeNumber:<n>". See PLAN.md §2026-09-06 for the verified shape.
#
# The search key is NOT in this repo. It is theirs, it is lifted from their JS
# bundle, this repo is public, and they can rotate it whenever they like. It
# lives in THINCART_WEGMANS_KEY (set in the systemd unit); unset simply means no
# price or aisle, which every screen must already handle for stores that have no
# price source at all.
# All four are theirs, not ours, and can change without telling us — so all four
# are overridable from the systemd unit (this repo has no .env; THINCART_TZ and
# THINCART_LOOKUP already live there). The defaults are what was verified on
# 2026-09-06 and mean the feature works out of the box once the key is set.
WEGMANS_APP = os.environ.get("THINCART_WEGMANS_APP", "QGPPR19V8V").strip()
WEGMANS_KEY = os.environ.get("THINCART_WEGMANS_KEY", "").strip()
WEGMANS_INDEX = os.environ.get("THINCART_WEGMANS_INDEX", "products").strip()
WEGMANS_STORE_URL = os.environ.get(
    "THINCART_WEGMANS_STORE_URL", "https://www.wegmans.com/stores/{slug}"
).strip()

# Re-exported: lookup_api.py and the tests reach the OSM parser through this
# module, and the parsing moved out (osm.py) rather than the callers.
GROCERY_TYPES = osm.GROCERY_TYPES
parse_store_results = osm.parse_store_results

_conn: sqlite3.Connection | None = None
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0


def bind(conn: sqlite3.Connection) -> None:
    global _conn
    _conn = conn


def enabled(feature: str) -> bool:
    """`feature` is 'stores' or one of the content lookups."""
    if MODE == "all":
        return True
    if MODE == "stores":
        return feature == "stores"
    return False


# 503 covers both "the owner switched this off" and "the provider fell over",
# which need opposite things said to the user — one is a setting, the other is
# weather. The status cannot tell them apart, so the body does.
DISABLED = "lookup_disabled"
UNAVAILABLE = "lookup_unavailable"


def _require(feature: str) -> None:
    if not enabled(feature):
        raise HTTPException(503, {"code": DISABLED, "mode": MODE})


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def cache_get(kind: str, key: str, max_age: timedelta | None = None) -> Any | None:
    """Returns the cached payload, or None when absent or too old.

    `max_age` overrides the kind's TTL. One cached record answers two questions
    with different shelf lives: where a thing sits barely changes, what it costs
    changes weekly. Serving a fortnight-old price because the aisle was still
    good is how a stale number gets shown as current.
    """
    if _conn is None:
        return None
    row = _conn.execute(
        "SELECT payload_json, fetched_at FROM lookup_cache WHERE kind=? AND key=?", (kind, key)
    ).fetchone()
    if row is None:
        return None
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except ValueError:
        return None
    if datetime.now(UTC) - fetched > (max_age or TTL.get(kind, timedelta(days=7))):
        return None
    return json.loads(row["payload_json"])


def cache_put(kind: str, key: str, payload: Any) -> None:
    if _conn is None:
        return
    _conn.execute(
        "INSERT OR REPLACE INTO lookup_cache(kind, key, payload_json, fetched_at) VALUES(?,?,?,?)",
        (kind, key, json.dumps(payload, ensure_ascii=False), now_iso()),
    )
    _conn.commit()


async def _nominatim(params: dict[str, str], url: str = "") -> list[dict] | None:
    """One rate-limited Nominatim call. None on any failure — never a guess.
    `url` selects the endpoint; /reverse answers with one place, and it comes
    back as a one-item list so every caller reads the same shape."""
    global _nominatim_last
    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (asyncio.get_running_loop().time() - _nominatim_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url or NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("nominatim lookup failed: %s", e)
            return None
        finally:
            _nominatim_last = asyncio.get_running_loop().time()
    if isinstance(data, dict):
        return [data] if "osm_id" in data else None
    return data if isinstance(data, list) else None


async def reverse_geocode(lat: float, lon: float) -> dict | None:
    """The place under a pin, in our store shape (address, town, postcode)."""
    raw = await _nominatim({"lat": str(lat), "lon": str(lon), "format": "jsonv2", "addressdetails": "1",
                            "zoom": "18"}, url=NOMINATIM_REVERSE_URL)
    out = osm.parse_store_results(raw) if raw else []
    if not out or not raw:
        return None
    # Under a pin there is usually no named shop, just a building or a road;
    # the parser would then take the house number for a name and cut it off
    # the address. Keep the whole address and claim no name.
    if not raw[0].get("name"):
        out[0]["name"] = ""
        out[0]["address"] = (raw[0].get("display_name") or "").strip()
    out[0]["postcode"] = str((raw[0].get("address") or {}).get("postcode") or "")
    return out[0]


async def follow_link(url: str) -> str | None:
    """Where a short link lands. Google's map links are only a redirect."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126"})
            return str(r.url)
    except Exception as e:
        log.warning("could not follow link: %s", e)
        return None


# --- Wegmans adapter ---------------------------------------------------------
#
# One chain, deliberately. A store either has a price source or it does not, and
# every screen has to read correctly in the second case — which is most stores.


async def _algolia(index: str, body: dict) -> list[dict] | None:
    """One query. None on any failure — never a guess, and never a partial
    result dressed up as a whole one."""
    if not WEGMANS_KEY:
        log.info("wegmans lookup skipped: THINCART_WEGMANS_KEY unset")
        return None
    headers = {
        "X-Algolia-Application-Id": WEGMANS_APP,
        "X-Algolia-API-Key": WEGMANS_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://{WEGMANS_APP}-dsn.algolia.net/1/indexes/{index}/query",
                json=body, headers=headers,
            )
            r.raise_for_status()
            return r.json().get("hits", [])
    except Exception as e:
        log.warning("wegmans query failed: %s", e)
        return None


async def wegmans_products(
    term: str, store_number: str, limit: int = 8, max_age: timedelta | None = None
) -> list[dict] | None:
    """Products matching `term` at ONE store, carrying that store's price and
    shelf position. None = the lookup failed; [] = it genuinely found nothing.
    Callers must keep those apart; they mean opposite things to the user.

    `max_age` is how stale the caller can bear: the price endpoint passes the
    price TTL so it never serves a fortnight-old amount that was cached for its
    aisle."""
    key = f"{store_number}|{term.strip().lower()}|{limit}"
    cached = cache_get("product", key, max_age)
    if cached is not None:
        return cached
    hits = await _algolia(
        WEGMANS_INDEX,
        {"query": term.strip(), "hitsPerPage": limit, "filters": f"storeNumber:{store_number}"},
    )
    if hits is None:
        return None
    parsed = parse_wegmans_hits(hits)
    cache_put("product", key, parsed)
    return parsed


async def wegmans_store_number(slug: str) -> str | None:
    """slug is e.g. 'princeton-nj'. Cached hard — a branch number is forever."""
    cached = cache_get("chain", slug)
    if cached is not None:
        return cached or None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(
                WEGMANS_STORE_URL.format(slug=slug), headers={"User-Agent": USER_AGENT}
            )
            r.raise_for_status()
            num = parse_store_number(r.text)
    except Exception as e:
        log.warning("wegmans store lookup failed: %s", e)
        return None
    if not num:
        # 200 but no number: a changed page shape, an interstitial, a bot wall.
        # That is a failure to read, not a branch that does not exist — caching
        # it under the 90-day chain TTL would freeze a transient into a fact.
        log.warning("wegmans branch page for %s parsed no store number", slug)
        return None
    cache_put("chain", slug, num)
    return num


async def _algolia_multi(bodies: list[dict]) -> list[list[dict]] | None:
    """Several queries in one round trip. An aisle walk asks the same question
    for every item on the list; doing that one request at a time is the
    difference between a usable screen and six seconds of spinner."""
    if not WEGMANS_KEY or not bodies:
        return None
    headers = {
        "X-Algolia-Application-Id": WEGMANS_APP,
        "X-Algolia-API-Key": WEGMANS_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = {"requests": [{"indexName": WEGMANS_INDEX, **b} for b in bodies]}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                f"https://{WEGMANS_APP}-dsn.algolia.net/1/indexes/*/queries",
                json=payload, headers=headers,
            )
            r.raise_for_status()
            return [res.get("hits", []) for res in r.json().get("results", [])]
    except Exception as e:
        log.warning("wegmans multi-query failed: %s", e)
        return None


async def wegmans_products_many(
    terms: list[str], store_number: str
) -> tuple[dict[str, list[dict]], bool]:
    """({term: records}, complete). Cache first; only the misses go over the wire.

    `complete` is False when the network leg failed and some terms were never
    asked about. It matters because the caller cannot tell the difference from
    the dict alone: a term that is absent because nobody could ask looks exactly
    like a term that genuinely has no aisle, and silently rendering the first as
    the second is the mistake rule 2 exists to prevent.
    """
    found: dict[str, list[dict]] = {}
    misses = []
    for term in terms:
        key = f"{store_number}|{term.strip().lower()}|5"
        hit = cache_get("product", key)
        if hit is not None:
            found[term] = hit
        else:
            misses.append(term)
    if not misses:
        return found, True
    if not terms:
        return {}, True          # an empty list is answered, not unavailable
    results = await _algolia_multi(
        [{"query": t.strip(), "hitsPerPage": 5, "filters": f"storeNumber:{store_number}"} for t in misses]
    )
    if results is None:
        return found, False
    if len(results) != len(misses):
        # Fewer answers than questions. The missing terms would otherwise vanish
        # silently and be reported as items with no aisle, while `complete` said
        # everything was asked — the two failure modes this pair exists to keep
        # apart, collapsed by a short list.
        log.warning("wegmans multi-query returned %d results for %d queries",
                    len(results), len(misses))
        return found, False
    stamp = now_iso()
    for term, hits in zip(misses, results, strict=False):
        parsed = parse_wegmans_hits(hits, stamp)
        cache_put("product", f"{store_number}|{term.strip().lower()}|5", parsed)
        found[term] = parsed
    return found, True


# --- Whole Foods and ShopRite: Chrome-shaped HTTP ----------------------------
#
# Both sit behind a client check: the URL a browser is served is refused to
# httpx, cookies and all, because the TLS handshake gives the client away.
# curl_cffi presents Chrome's handshake — no browser, no challenge to solve, and
# the request is otherwise the one their own site makes (PLAN.md §2026-09-07).


async def _chrome_get(url: str, *, headers: dict | None = None, cookies: dict | None = None,
                      params: dict | None = None, timeout: int = 25) -> tuple[int, str] | None:
    """(status, body), or None when the request itself failed."""
    try:
        async with AsyncSession(impersonate="chrome") as s:
            r = await s.get(url, headers=headers or {}, cookies=cookies or {},
                            params=params, timeout=timeout)
            return r.status_code, r.text
    except Exception as e:
        log.warning("request failed (%s): %s", url.split("?")[0][:90], e)
        return None


async def wholefoods_store(slug: str) -> dict | None:
    """The branch behind /stores/<slug>. Cached under the slug AND the code,
    so the cookie that selects the store can be rebuilt from the code alone."""
    cached = cache_get("chain", f"wholefoods:{slug}")
    if cached:
        return cached
    got = await _chrome_get(wholefoods.STORE_URL.format(slug=slug), headers={"accept": "text/html"})
    if got is None or got[0] != 200:
        return None
    store = wholefoods.parse_store(got[1])
    if not store:
        # 200 but no code: a failure to read, not a branch that does not exist.
        log.warning("whole foods store page for %s parsed no store code", slug)
        return None
    cache_put("chain", f"wholefoods:{slug}", store)
    cache_put("chain", f"wholefoods:code:{store['code']}", store)
    return store


def _wf_cookies(store_code: str) -> dict[str, str]:
    st = cache_get("chain", f"wholefoods:code:{store_code}") or {}
    return {"wfm_store_d8": wholefoods.store_cookie(
        store_code, st.get("folder", ""), st.get("name", ""), st.get("state", ""))}


async def _wf_search(term: str, store_code: str) -> list[dict] | None:
    """Measured 2026-09-07: Amazon answers about half of these with a well-formed
    page listing nothing, and the same query a second later with thirty
    products — and a genuine no-result page is the same shape. So an empty page
    is asked again, and one that stays empty is UNAVAILABLE, never "the shop
    has none", which would be cached for a fortnight and shown as fact."""
    for attempt in range(3):
        got = await _chrome_get(wholefoods.SEARCH_URL.format(term=quote(term.strip())),
                                headers={"accept": "text/html"}, cookies=_wf_cookies(store_code))
        if got is None or got[0] != 200:
            return None
        recs = wholefoods.parse_search(got[1], store_code)
        if recs:
            return recs
        if recs is None:
            return None
        await asyncio.sleep(0.5 * (attempt + 1))
    log.info("whole foods search for %r at %s came back empty three times", term, store_code)
    return None


async def _wf_location(store_code: str, sku: str) -> dict | None:
    got = await _chrome_get(wholefoods.PRODUCT_URL.format(asin=sku),
                            headers={"accept": "text/html"}, cookies=_wf_cookies(store_code))
    if got is None or got[0] != 200:
        return None
    loc = wholefoods.parse_location(got[1])
    return None if loc is None else ({"aisle": loc} if loc else {})


_SR_SESSION = str(uuid.uuid4())


def _sr_headers() -> dict[str, str]:
    return shoprite.headers(_SR_SESSION, str(uuid.uuid4()))


async def shoprite_stores() -> dict | None:
    """Every branch, trimmed to what find_branch reads. One request a month."""
    cached = cache_get("chain", "shoprite:stores")
    if cached:
        return cached
    got = await _chrome_get(f"{shoprite.GATEWAY}/api/stores", headers=_sr_headers())
    if got is None or got[0] != 200:
        return None
    try:
        items = json.loads(got[1]).get("items") or []
    except ValueError:
        return None
    keep = ("id", "name", "retailerStoreId", "addressLine1", "city", "countyProvinceState", "postCode", "type")
    trimmed = {"items": [{k: s.get(k) for k in keep} for s in items]}
    if not trimmed["items"]:
        return None
    cache_put("chain", "shoprite:stores", trimmed)
    return trimmed


async def _sr_search(term: str, rsid: str, limit: int) -> list[dict] | None:
    got = await _chrome_get(f"{shoprite.GATEWAY}/api/stores/{rsid}/search", headers=_sr_headers(),
                            params={"q": term.strip(), "take": limit, "skip": 0})
    if got is None or got[0] != 200:
        return None
    try:
        return shoprite.parse_search(json.loads(got[1]), rsid)
    except ValueError:
        return None


async def _sr_location(rsid: str, sku: str) -> dict | None:
    got = await _chrome_get(f"{shoprite.GATEWAY}/api/stores/{rsid}/products/{sku}", headers=_sr_headers())
    if got is None or got[0] != 200:
        return None
    try:
        return shoprite.parse_location(json.loads(got[1]))
    except ValueError:
        return None


_LOCATE = {"wholefoods": _wf_location, "shoprite": _sr_location}


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
        loc = cache_get("aisle", key)
        if loc is None:
            loc = await _LOCATE[chain](store, rec["sku"])
            if loc is None:
                continue
            cache_put("aisle", key, loc)
        new = (loc.get("aisle", ""), loc.get("shelf", ""))
        if new != (rec.get("aisle", ""), rec.get("shelf", "")):
            rec["aisle"], rec["shelf"] = new
            changed = True
    return changed


async def _chain_products(chain: str, term: str, store: str, limit: int,
                          max_age: timedelta | None, prefer: list[str]) -> list[dict] | None:
    """Search-then-place. The search is cached once, when fetched, and never
    written back: positions live in their own cache and are laid on at every
    read, which costs nothing — writing placed records back would re-stamp the
    row, and a fortnight-old price would pass the two-day check as fresh."""
    key = f"{chain}:{store}|{term.strip().lower()}|{limit}"
    recs = cache_get("product", key, max_age)
    if recs is None:
        fetched = await (_wf_search(term, store) if chain == "wholefoods" else _sr_search(term, store, limit))
        if fetched is None:
            return None
        recs = fetched[:limit]
        cache_put("product", key, recs)
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
        return await wegmans_products(term, store, limit, max_age)
    if chain in _LOCATE:
        prefer = [prefer_sku] if isinstance(prefer_sku, str) else list(prefer_sku or [])
        return await _chain_products(chain, term, store, limit, max_age, [s for s in prefer if s])
    return None


async def products_many(chain: str, terms: list[str], store: str,
                        prefer: dict[str, list[str]] | None = None) -> tuple[dict[str, list[dict]], bool]:
    """({term: records}, complete) — see wegmans_products_many for why
    `complete` exists. The two page-backed chains have no multi-query, so this
    asks term by term, a few at a time, and reports any it could not ask.
    `prefer` maps a term to EVERY sku picked under it: two items can share a
    search term and mean different jars, and each must find its shelf."""
    if chain == "wegmans":
        return await wegmans_products_many(terms, store)
    if chain not in _LOCATE:
        return {}, False
    sem = asyncio.Semaphore(3)

    async def one(t: str) -> tuple[str, list[dict] | None]:
        async with sem:
            return t, await products(chain, t, store, limit=5, prefer_sku=(prefer or {}).get(t))

    found: dict[str, list[dict]] = {}
    complete = True
    for t, recs in await asyncio.gather(*(one(t) for t in dict.fromkeys(terms))):
        if recs is None:
            complete = False
        else:
            found[t] = recs
    return found, complete
