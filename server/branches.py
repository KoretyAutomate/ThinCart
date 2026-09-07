"""
branches.py — naming the branch a pinned store IS, chain by chain.

No network of its own: the fetchers it calls live in lookup.py, which stays the
only module that reaches outward. What lives here is the judgement — when a
chain's answer counts as THIS store, and when it must be refused — split out of
lookup.py on 2026-09-07 when that file reached the 600-line ceiling.

The one rule, from every angle: never a wrong branch. A wrong branch prices and
places everything with no visible sign, and the household would trust it.
"""

import shoprite
import wholefoods
from chains import close, postcode, town_from_name
from lookup import shoprite_stores, wegmans_store_number, wholefoods_store
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
    slug = wegmans_slug(address)
    if not slug:
        return {"chain_store_id": "", "reason": "could not read a town from the address"}
    num = await wegmans_store_number(slug)
    return {"chain_store_id": num or "", "reason": "" if num else f"no Wegmans branch page for '{slug}'"}


async def _wholefoods(pin: dict, address: str, named: str) -> dict:
    slug = wholefoods.wholefoods_slug(address) or named
    if not slug:
        return {"chain_store_id": "", "reason": "could not read a town from the address or the name"}
    store = await wholefoods_store(slug)
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
    zipc = postcode(address)
    same = (bool(zipc) and store.get("postcode") == zipc) or close(
        pin.get("lat"), pin.get("lon"), store.get("lat"), store.get("lon"))
    if not same:
        where = f"{store.get('name') or slug} {store.get('postcode') or ''}".strip()
        return {"chain_store_id": "",
                "reason": f"the Whole Foods page for '{slug}' is the branch at {where}, not this pin"}
    return {"chain_store_id": store["code"], "reason": ""}


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
