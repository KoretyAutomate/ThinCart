"""
shoprite.py — reading ShopRite's storefront gateway. No network: every function
here turns a payload into our shape, or a string into another string.

Same seam as wegmans.py and wholefoods.py: lookup.py makes the requests, this
module knows what the answers mean, and the tests drive it from RECORDED
responses.

What was learned (PLAN.md §2026-09-07):

  - The shop runs on Mi9 Retail. The JSON API is `storefrontgateway.shoprite.com`
    and every branch is addressed by its `retailerStoreId` — "500" is
    Lawrenceville. `3000` is the corporate placeholder the site opens on, and it
    has no shelves: its products come back with `productLocation: null`.
  - The gateway answers only when four headers the site's own app sends are
    present: `x-site-host`, `x-shopping-mode`, `x-customer-session-id` and
    `x-correlation-id`. Without them every real path is a 404, which is how
    eight correct URLs were mistaken for wrong ones. The session and
    correlation ids can be freshly generated; the shopping mode is a fixed id.
  - `/api/stores/{rsid}/search?q=` carries the branch's price on every hit.
    `/api/stores/{rsid}/products/{sku}` carries `productLocation`:
    `{"aisle": "9", "shelf": "7"}` for centre-store goods, a department name
    ("DAIRY DEPARTMENT") for perishables.
  - `/api/stores` lists all 315 branches with address and ZIP.
"""

import os
import re
from datetime import UTC, datetime
from urllib.parse import quote_plus

from chains import US_STATES, address_town_state, postcode

# Theirs, not ours, and overridable from the systemd unit like the Wegmans
# endpoints in lookup.py — a redesign or a proxy is a config change, not a
# code change. The defaults are what was verified on 2026-09-07.
GATEWAY = os.environ.get("THINCART_SHOPRITE_GATEWAY", "https://storefrontgateway.shoprite.com").strip().rstrip("/")
SITE = os.environ.get("THINCART_SHOPRITE_SITE", "https://www.shoprite.com").strip().rstrip("/")
# A page a human can open and check the number against. The gateway has no
# public product page keyed by sku alone, so the branch's search for the
# product's own name is the checkable thing.
PRODUCT_URL = SITE + "/sm/pickup/rsid/{rsid}/search?q={name}"
SHOPPING_MODE = "11111111-1111-1111-1111-111111111111"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def headers(session_id: str, correlation_id: str) -> dict[str, str]:
    """The four headers without which the gateway answers nothing."""
    return {
        "accept": "application/json, text/plain, */*",
        "origin": SITE,
        "referer": SITE + "/",
        "x-site-host": SITE,
        "x-shopping-mode": SHOPPING_MODE,
        "x-customer-session-id": f"{SITE}|{session_id}",
        "x-correlation-id": correlation_id,
    }


def _title(s: str) -> str:
    """'DAIRY DEPARTMENT' -> 'Dairy Department'. The gateway shouts department
    names; nothing else on the screen does."""
    return " ".join(w.capitalize() for w in s.split())


def parse_search(payload: dict, rsid: str, stamp: str | None = None) -> list[dict]:
    """Search hits -> our records, in the gateway's ranking. Each carries the
    branch's price and no aisle; that is on the product record and lookup.py
    fills it for the products that matter (see `parse_location`)."""
    stamp = stamp or now_iso()
    out = []
    for h in payload.get("items") or []:
        sku = str(h.get("sku") or h.get("productId") or "").strip()
        name = (h.get("name") or "").strip()
        amount = h.get("priceNumeric")
        if amount is None:
            m = re.search(r"\d+(?:\.\d+)?", str(h.get("price") or ""))
            amount = float(m.group(0)) if m else None
        if not sku or not name or amount is None:
            continue
        size = h.get("unitOfSize") or {}
        pack = ""
        if size.get("size") is not None and size.get("abbreviation"):
            n = size["size"]
            pack = f"{int(n) if float(n).is_integer() else n} {size['abbreviation']}"
        out.append({
            "sku": sku,
            "name": name,
            "brand": (h.get("brand") or "").strip(),
            "sub_brand": "",
            "pack_size": pack,
            "upc": sku if sku.isdigit() else "",
            "store_number": str(rsid),
            "amount": float(amount),
            "unit_price": (h.get("pricePerUnit") or "").strip(),
            "aisle": "", "aisle_side": "", "section": "", "shelf": "",
            "available": bool(h.get("available", True)),
            "source": "shoprite",
            # quote_plus, because "Bowl & Basket" is most of their own-brand
            # range and a bare ampersand would end the query at "Bowl".
            "source_url": PRODUCT_URL.format(rsid=rsid, name=quote_plus(name)),
            "fetched_at": stamp,
        })
    return out


def parse_location(payload: dict) -> dict | None:
    """{"aisle": "9", "shelf": "7"} or {"aisle": "Dairy Department"} from a
    product record; {} when the record says the item has no place (the corporate
    store, an online-only line); None when the payload is not a product record
    at all. Absence and unreadability stay apart."""
    if not isinstance(payload, dict) or "productLocation" not in payload:
        return None
    loc = payload.get("productLocation") or {}
    aisle = str(loc.get("aisle") or "").strip()
    if not aisle:
        return {}
    out = {"aisle": aisle if aisle[:1].isdigit() else _title(aisle)}
    shelf = str(loc.get("shelf") or "").strip()
    if shelf:
        out["shelf"] = shelf
    return out


def find_branch(stores: dict, address: str) -> dict | None:
    """The branch at an OpenStreetMap address: {"rsid", "name"}, or None.

    ZIP first — the one key two branches never share — then town and state,
    and only when exactly ONE branch is in that town: Hamilton Township has
    two, and the first of them is not an answer. The corporate row is skipped:
    it is where the site opens, it has an address, and it has no shelves. Never
    a nearest-guess; a wrong branch mis-prices everything with no visible sign.
    """
    rows = [s for s in stores.get("items") or [] if (s.get("type") or "") != "Corporate"
            and s.get("retailerStoreId")]
    zipc = postcode(address)
    if zipc:
        for s in rows:
            if str(s.get("postCode") or "")[:5] == zipc:
                return {"rsid": str(s["retailerStoreId"]), "name": s.get("name") or ""}
    town, state = address_town_state(address)
    if not town:
        return None
    # The list writes the state both ways ("New Jersey" on some rows, "NJ" on
    # others), so either spelling of ours has to match either of theirs.
    spellings = {state, {v: k for k, v in US_STATES.items()}.get(state, state)}
    in_town = []
    for s in rows:
        city = re.sub(r"[^a-z0-9]+", "-", str(s.get("city") or "").lower()).strip("-")
        st = str(s.get("countyProvinceState") or "").lower().strip()
        if city == town and st in spellings:
            in_town.append({"rsid": str(s["retailerStoreId"]), "name": s.get("name") or ""})
    return in_town[0] if len(in_town) == 1 else None
