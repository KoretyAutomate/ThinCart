"""
lookup.py — the ONE module in ThinCart allowed to make an outbound request.

Until Phase 6 this app made none at all: everything lived on the tailnet and the
only non-local dependency was vLLM on 127.0.0.1. Store search, price comparison
and aisle hints change that, and the household's shopping content is what
travels. Concentrating every outbound call here is what makes that reviewable —
an `httpx` call anywhere else in this repo is a review failure, not a style nit.

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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException

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

# OSM classes that are somewhere you buy groceries. Used to rank, not to filter:
# a shop tagged oddly must still be findable, it just sorts below the obvious.
GROCERY_TYPES = {
    "supermarket", "convenience", "grocery", "greengrocer", "butcher",
    "bakery", "deli", "farm", "seafood", "health_food", "wholesale",
}

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


async def _nominatim(params: dict[str, str]) -> list[dict] | None:
    """One rate-limited Nominatim call. None on any failure — never a guess."""
    global _nominatim_last
    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (asyncio.get_running_loop().time() - _nominatim_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("nominatim lookup failed: %s", e)
            return None
        finally:
            _nominatim_last = asyncio.get_running_loop().time()
    return data if isinstance(data, list) else None


def _town(addr: dict) -> str:
    """The most town-like field Nominatim gave us, or ''."""
    for k in ("city", "town", "village", "municipality", "suburb", "hamlet"):
        if addr.get(k):
            return str(addr[k]).strip()
    return ""


def parse_store_results(raw: list[dict], stamp: str | None = None) -> list[dict]:
    """Nominatim jsonv2 -> our shape. Pure, so the tests can drive it from a
    recorded fixture with no network. Anything without an osm_id is dropped:
    the whole point of this feature is an identity we can pin a store to."""
    stamp = stamp or now_iso()
    out = []
    for r in raw:
        osm_id = r.get("osm_id")
        osm_type = r.get("osm_type")
        if osm_id is None or not osm_type:
            continue
        name = (r.get("name") or "").strip() or (r.get("display_name") or "").split(",")[0].strip()
        if not name:
            continue
        addr = (r.get("display_name") or "").strip()
        # Drop the leading name from the address line; it is already the title.
        if addr.startswith(name + ","):
            addr = addr[len(name) + 1 :].strip()
        out.append(
            {
                "osm_id": f"{osm_type}/{osm_id}",
                "name": name,
                "address": addr,
                "lat": float(r["lat"]) if r.get("lat") else None,
                "lon": float(r["lon"]) if r.get("lon") else None,
                "brand": (r.get("extratags") or {}).get("brand", "") or "",
                # Nominatim already knows the town (addressdetails=1). It is what
                # tells two branches of one chain apart, and guessing it out of
                # the display string is guesswork this does not have to do.
                "town": _town(r.get("address") or {}),
                "kind": r.get("type") or "",
                "source": "openstreetmap",
                # Stamped at parse time and carried through the cache, so a
                # store identity can always say when it was learned. The module
                # contract is source AND fetched_at on every record; this was
                # the one place that only had the first half.
                "fetched_at": stamp,
            }
        )
    out.sort(key=lambda d: (d["kind"] not in GROCERY_TYPES, d["name"]))
    return out


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
