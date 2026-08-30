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
from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger("thincart.lookup")
router = APIRouter()

# off = no outbound calls at all (the pre-Phase-6 behaviour, a supported state).
# stores = only the store registry may reach out; the query is a shop name and a
# town, and says nothing about what the household buys.
# all = store search, product search, prices and aisle hints.
MODE = os.environ.get("THINCART_LOOKUP", "all").strip().lower()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent and at most one
# request a second. Both are conditions of being allowed to use it at all.
USER_AGENT = "ThinCart/1.0 (self-hosted household shopping list; +https://github.com/KoretyAutomate/ThinCart)"
NOMINATIM_MIN_INTERVAL = 1.1

TTL = {
    "store": timedelta(days=30),    # a shop's address effectively never moves
    "product": timedelta(days=14),
    "aisle": timedelta(days=60),    # layouts change on the order of months
    "price": timedelta(days=2),     # the one thing that genuinely moves
}

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


def _require(feature: str) -> None:
    if not enabled(feature):
        raise HTTPException(503, f"lookup disabled (THINCART_LOOKUP={MODE})")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def cache_get(kind: str, key: str) -> Any | None:
    """Returns the cached payload, or None when absent or past its TTL."""
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
    if datetime.now(UTC) - fetched > TTL.get(kind, timedelta(days=7)):
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


def parse_store_results(raw: list[dict]) -> list[dict]:
    """Nominatim jsonv2 -> our shape. Pure, so the tests can drive it from a
    recorded fixture with no network. Anything without an osm_id is dropped:
    the whole point of this feature is an identity we can pin a store to."""
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
                "kind": r.get("type") or "",
                "source": "openstreetmap",
            }
        )
    out.sort(key=lambda d: (d["kind"] not in GROCERY_TYPES, d["name"]))
    return out


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
        raise HTTPException(503, "store search unavailable")
    results = parse_store_results(raw)
    cache_put("store", key, results)
    return {"results": results, "source": "openstreetmap", "cached": False}
