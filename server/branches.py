"""
branches.py — naming the branch a pinned store IS, chain by chain.

No network of its own: the fetchers it calls live in lookup.py, which stays the
only module that reaches outward. What lives here is the judgement — when a
chain's answer counts as THIS store, and when it must be refused — split out of
lookup.py on 2026-09-07 when that file reached the 600-line ceiling.

The one rule, from every angle: never a wrong branch. A wrong branch prices and
places everything with no visible sign, and the household would trust it.
"""

import links
import shoprite
import wholefoods
from chains import CHAINS, address_town_state, close, detect, postcode, town_from_name
from lookup import follow_link, reverse_geocode, shoprite_stores, wegmans_store_number, wholefoods_store
from wegmans import wegmans_slug


async def resolve_branch(chain: str, pin: dict) -> dict:
    """{"chain_store_id", "reason", and for an unpinned store the branch's own
    "address"/"lat"/"lon"} — `pin` is the store row: name, OSM address and
    coordinates. The id is "" when the branch could not be named, and the
    reason says why in words the owner can act on.

    A store OpenStreetMap has never heard of — a branch that opened last month —
    has no address to resolve from. Then the town in the NAME the household
    typed is the clue, and the chain's own directory supplies the identity:
    its address and coordinates come back so the row can be pinned by them.
    """
    address = pin.get("address") or ""
    named = town_from_name(pin.get("name") or "", chain)
    if chain == "wegmans":
        return await _wegmans(address)
    if chain == "wholefoods":
        return await _wholefoods(pin, address, named)
    if chain == "shoprite":
        return await _shoprite(address, named)
    return {"chain_store_id": "", "reason": "no price adapter for this store"}


async def _wegmans(address: str) -> dict:
    town, state = address_town_state(address)
    if not town:
        return {"chain_store_id": "", "reason": "could not read a town from the address"}
    # "Montgomery Township" on the map is "montgomery" to the chain; try both.
    for t in links.town_candidates(town):
        num = await wegmans_store_number(f"{t}-{state}")
        if num:
            return {"chain_store_id": num, "reason": ""}
    return {"chain_store_id": "", "reason": f"no Wegmans branch page for '{wegmans_slug(address)}'"}


async def _wholefoods(pin: dict, address: str, named: str) -> dict:
    slug = wholefoods.wholefoods_slug(address) or named
    if not slug:
        return {"chain_store_id": "", "reason": "could not read a town from the address or the name"}
    # The map says "montgomery-township"; the chain's page is /stores/montgomery.
    # Every candidate page that answers is still checked against the pin below,
    # so trying more than one cannot pick a wrong branch, only find the right one.
    store = None
    for cand in links.town_candidates(slug):
        store = await wholefoods_store(cand)
        if store and (not address or _same_place(store, pin, address)):
            slug = cand
            break
    if not store:
        return {"chain_store_id": "", "reason": f"no Whole Foods store page for '{slug}'"}
    if not address:
        # Nothing to check the page against, and /stores/<town> is ONE store in
        # a town that may have several. The chain's directory supplies an
        # identity; the household confirms it is the one they meant before it
        # is pinned — `confirm` is what the phone puts in front of them.
        where = " ".join(f"{store.get('name') or slug.title()}, {store.get('state') or ''} "
                         f"{store.get('postcode') or ''}".split())
        return {"chain_store_id": store["code"], "reason": "", "address": where, "confirm": where,
                "lat": store.get("lat"), "lon": store.get("lon")}
    # /stores/<town> is ONE store, and a town can have several. The page names
    # its own ZIP and coordinates; the pin must match one of them, or the page
    # is some other branch and its prices would be too.
    if not _same_place(store, pin, address):
        where = f"{store.get('name') or slug} {store.get('postcode') or ''}".strip()
        return {"chain_store_id": "",
                "reason": f"the Whole Foods page for '{slug}' is the branch at {where}, not this pin"}
    return {"chain_store_id": store["code"], "reason": ""}


def _same_place(store: dict, pin: dict, address: str) -> bool:
    zipc = postcode(address)
    return (bool(zipc) and store.get("postcode") == zipc) or close(
        pin.get("lat"), pin.get("lon"), store.get("lat"), store.get("lon"))


async def _shoprite(address: str, named: str) -> dict:
    stores = await shoprite_stores()
    if stores is None:
        return {"chain_store_id": "", "reason": "could not read ShopRite's store list"}
    branch = shoprite.find_branch(stores, address, town=named)
    if not branch:
        return {"chain_store_id": "", "reason": "no ShopRite branch at that address"
                if address else f"no single ShopRite branch in '{named}'"}
    out = {"chain_store_id": branch["rsid"], "reason": ""}
    if not address and branch.get("address"):
        # Unique in its town, but still a branch the household never pointed
        # at: they confirm it, the same as for Whole Foods.
        out["address"] = out["confirm"] = f"{branch['name']} — {branch['address']}"
    return out


# --- a pasted link ---------------------------------------------------------------


async def store_from_link(url: str, price_ok: bool) -> dict:
    """{"result": a store hit in the OSM-search shape (+ chain fields), or None,
    "reason"}. For the shop OpenStreetMap has never heard of: a Google Maps pin
    or the chain's own store page, which a person chose while looking at the
    right shop. `price_ok` is whether the chain may be contacted at all
    (THINCART_LOOKUP=stores promises OpenStreetMap and nothing else)."""
    parsed = links.parse_link(url)
    if parsed and parsed["kind"] == "short":
        final = await follow_link(parsed["url"])
        parsed = links.parse_link(final or "")
        if parsed and parsed["kind"] == "short":
            parsed = None
    if not parsed:
        return {"result": None,
                "reason": "not a link this can read — a Google Maps place link, or a chain's store page"}
    if parsed["kind"] == "chain":
        if not price_ok:
            return {"result": None, "reason": "price lookups are off, so a chain's page cannot be read"}
        return await _from_chain_link(parsed)
    return await _from_pin(parsed, price_ok)


async def _from_chain_link(p: dict) -> dict:
    chain = p["chain"]
    label = CHAINS[chain].label
    if chain == "wholefoods":
        store = await wholefoods_store(p["slug"])
        if not store:
            return {"result": None, "reason": f"no Whole Foods store page for '{p['slug']}'"}
        town = store.get("name") or p["slug"].replace("-", " ").title()
        hit = {"name": f"Whole Foods Market {town}", "address": " ".join(
            f"{town}, {store.get('state') or ''} {store.get('postcode') or ''}".split()),
               "lat": store.get("lat"), "lon": store.get("lon"), "town": town,
               "osm_id": f"wholefoods:{store['code']}", "chain_store_id": store["code"]}
    elif chain == "wegmans":
        num = await wegmans_store_number(p["slug"])
        if not num:
            return {"result": None, "reason": f"no Wegmans branch page for '{p['slug']}'"}
        parts = p["slug"].rsplit("-", 1)
        town = parts[0].replace("-", " ").title()
        hit = {"name": f"Wegmans {town}", "address": f"{town}, {parts[1].upper()}" if len(parts) == 2 else town,
               "lat": None, "lon": None, "town": town, "osm_id": f"wegmans:{num}", "chain_store_id": num}
    else:
        stores = await shoprite_stores()
        if stores is None:
            return {"result": None, "reason": "could not read ShopRite's store list"}
        row = next((s for s in stores["items"] if str(s.get("retailerStoreId")) == p["rsid"]), None)
        if not row:
            return {"result": None, "reason": f"no ShopRite branch numbered {p['rsid']}"}
        addr = ", ".join(str(row.get(k) or "") for k in ("addressLine1", "city", "countyProvinceState", "postCode")
                         if row.get(k))
        hit = {"name": row.get("name") or f"ShopRite {p['rsid']}", "address": addr, "lat": None, "lon": None,
               "town": row.get("city") or "", "osm_id": f"shoprite:{p['rsid']}", "chain_store_id": p["rsid"]}
    hit.update({"brand": label, "kind": "supermarket", "source": chain, "chain": chain})
    return {"result": hit, "reason": ""}


async def _from_pin(p: dict, price_ok: bool) -> dict:
    lat, lon = p["lat"], p["lon"]
    place = await reverse_geocode(lat, lon)
    address = (place or {}).get("address") or ""
    name = p.get("name") or (place or {}).get("name") or ""
    if not name:
        name = f"Store at {address.split(',')[0].strip()}" if address else f"Store at {lat:.4f}, {lon:.4f}"
    hit = {"name": name, "address": address, "lat": lat, "lon": lon, "town": (place or {}).get("town") or "",
           "brand": "", "kind": "supermarket", "osm_id": f"geo:{lat:.6f},{lon:.6f}", "source": "google-maps",
           "chain": "", "chain_store_id": "", "link_reason": ""}
    chain = detect(name)
    if chain and price_ok:
        found = await resolve_branch(chain, {"name": name, "address": address, "lat": lat, "lon": lon})
        if found["chain_store_id"]:
            hit.update({"chain": chain, "chain_store_id": found["chain_store_id"], "brand": CHAINS[chain].label})
        else:
            hit["link_reason"] = found["reason"]
    elif chain:
        hit["link_reason"] = "price lookups are off"
    if not place:
        hit["link_reason"] = hit["link_reason"] or "could not read an address for that pin"
    return {"result": hit, "reason": ""}
