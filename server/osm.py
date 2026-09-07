"""
osm.py — reading OpenStreetMap's answers. No network: Nominatim jsonv2 -> our
store shape, and the ranking that puts grocers first.

Moved out of lookup.py on 2026-09-07 when two more chains arrived and that file
needed the room under the 600-line ceiling. Same seam as wegmans.py,
wholefoods.py and shoprite.py: lookup.py owns the outbound policy and makes the
request; this module knows what the answer means, and is pure so the tests can
pin it against a recorded response.
"""

from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# OSM classes that are somewhere you buy groceries. Used to rank, not to filter:
# a shop tagged oddly must still be findable, it just sorts below the obvious.
GROCERY_TYPES = {
    "supermarket", "convenience", "grocery", "greengrocer", "butcher",
    "bakery", "deli", "farm", "seafood", "health_food", "wholesale",
}


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
