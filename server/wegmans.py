"""
wegmans.py — reading one chain's product data. No network: every function here
turns a payload into our shape, or a string into another string.

Split out of lookup.py when it crossed the repo's 600-line ceiling, the same way
ideas.py was split out of app.py. The split is along a real seam rather than an
arbitrary one: lookup.py owns the outbound-request POLICY — the kill switch, the
cache, the rate limit — and remains the only module in this repo that makes a
request at all. This one owns knowing what the answers mean.

Keeping it pure is what lets the tests pin the parsing against recorded
responses without touching the network, which is the lesson the APK taught
(PLAN.md §2026-08-30): a suite that only exercises one side of a seam proves
nothing about the other.
"""

import re
from datetime import UTC, datetime

from chains import address_town_state, aisle_label

# aisle_label moved to chains.py on 2026-09-07 when it became every chain's
# label rather than this one's; it stays reachable here under its old name.
__all__ = ["aisle_label", "parse_store_number", "parse_wegmans_hits", "wegmans_slug"]


def now_iso() -> str:
    """Local copy rather than an import from lookup.py: this module stays free
    of that one so the dependency runs one way only, policy -> parsing."""
    return datetime.now(UTC).isoformat()

PRODUCT_URL ="https://www.wegmans.com/shop/product/{sku}"


def parse_wegmans_hits(hits: list[dict], stamp: str | None = None) -> list[dict]:
    """Algolia `products` hits -> our shape. Pure, so tests drive it from a
    recorded response with no network.

    A hit with neither a price nor a shelf position is dropped: it answers
    neither question, and a row rendering as two blanks reads as "free, location
    unknown" rather than as the absence it actually is.
    """
    stamp = stamp or now_iso()
    out = []
    for h in hits:
        sku = str(h.get("skuId") or "").strip()
        name = (h.get("productName") or "").strip()
        if not sku or not name:
            continue
        price = h.get("price_inStore") or {}
        amount = price.get("amount")
        plan = h.get("planogram") or {}
        aisle = str(plan.get("aisle") or "").strip()
        if amount is None and not aisle:
            continue
        upc = h.get("upc")
        out.append(
            {
                "sku": sku,
                "name": name,
                "brand": (h.get("consumerBrandName") or "").strip(),
                "sub_brand": (h.get("consumerSubBrandName") or "").strip(),
                "pack_size": (h.get("packSize") or "").strip(),
                "upc": (upc[0] if isinstance(upc, list) and upc else upc) or "",
                "store_number": str(h.get("storeNumber") or "").strip(),
                "amount": float(amount) if amount is not None else None,
                "unit_price": (price.get("unitPrice") or "").strip(),
                "aisle": aisle,
                "aisle_side": str(plan.get("aisleSide") or "").strip(),
                "section": str(plan.get("section") or "").strip(),
                "shelf": str(plan.get("shelf") or "").strip(),
                "available": bool(h.get("isAvailable")),
                "source": "wegmans",
                # A source a human can open and check the number against. The
                # contract this module works to says every displayed price
                # carries one; "wegmans" names the source but cannot be looked at.
                "source_url": PRODUCT_URL.format(sku=sku),
                "fetched_at": stamp,
            }
        )
    return out

def parse_store_number(html: str) -> str:
    """The chain's own branch number, off its store page. That number is what
    the product index is keyed by; the OSM id that pins the store is a different
    identity and cannot substitute for it."""
    m = re.search(r'storeNumber\\?"?\s*:\s*\\?"?(\d+)', html)
    return m.group(1) if m else ""

def wegmans_slug(address: str) -> str:
    """'240 Nassau Park Blvd, Princeton, Mercer County, New Jersey, 08540, ...'
    -> 'princeton-nj'. Empty when the address does not parse — never a guessed
    slug, which would resolve to some other town's branch and mis-price
    everything with no visible sign of it."""
    town, state = address_town_state(address)
    return f"{town}-{state}" if town else ""
