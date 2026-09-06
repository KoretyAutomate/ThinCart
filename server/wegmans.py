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


def now_iso() -> str:
    """Local copy rather than an import from lookup.py: this module stays free
    of that one so the dependency runs one way only, policy -> parsing."""
    return datetime.now(UTC).isoformat()

US_STATES = {
    "new york": "ny", "pennsylvania": "pa", "new jersey": "nj", "virginia": "va",
    "maryland": "md", "massachusetts": "ma", "delaware": "de", "north carolina": "nc",
    "connecticut": "ct", "district of columbia": "dc",
}

PRODUCT_URL = "https://www.wegmans.com/shop/product/{sku}"


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

def aisle_label(rec: dict) -> str:
    """"Aisle 14B · left · sec 11" — a findable instruction rather than a bare
    number. Empty when there is no aisle, which callers must render as unknown
    rather than as a plausible blank."""
    if not rec.get("aisle"):
        return ""
    # The field is not always a number: perishables come back as a department
    # ("Dairy", "Produce"). "Aisle Dairy" reads as a mistake, so the word is
    # only added where it is actually an aisle.
    head = rec["aisle"]
    parts = [f"Aisle {head}" if head[:1].isdigit() else head]
    side = {"L": "left", "R": "right"}.get((rec.get("aisle_side") or "").upper())
    if side:
        parts.append(side)
    if rec.get("section"):
        parts.append(f"sec {rec['section']}")
    return " · ".join(parts)

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
    parts = [p.strip() for p in address.split(",")]
    state = idx = None
    for i, p in enumerate(parts):
        if p.lower() in US_STATES:
            state, idx = US_STATES[p.lower()], i
            break
    if state is None or idx is None:
        return ""
    for j in range(idx - 1, -1, -1):
        if parts[j].lower().endswith("county"):
            continue
        town = re.sub(r"[^a-z0-9]+", "-", parts[j].lower()).strip("-")
        if town:
            return f"{town}-{state}"
    return ""
