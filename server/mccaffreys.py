"""
mccaffreys.py — reading McCaffrey's Express, McCaffrey's own online shop. No
network: every function here turns a payload into our shape. lookup.py makes
the requests; the tests drive this from RECORDED responses.

What was learned (PLAN.md §2026-10-10):

  - The shop (express.mccaffreys.com) runs on ECRS's web store. Its JSON API
    answers plain HTTPS with no key, cookie or session.
  - Every branch is a path prefix: `/s/<store id>/api/b` searches THAT branch
    ("1000-7" is Princeton). POST {"q": term, "pn": page, "ps": page size}.
  - Each hit carries the branch's price (`actualPrice`, for `actualPriceDivider`
    units — "10 for $10" is 10.0 / 10), the size ("32 OZ", "12 FZ", "1 LB" for
    goods sold by weight, priced per pound) and the shelf: "2 R" is aisle 2,
    right side; "PRODUCE" or "MEAT" is a department.
  - `/api/stores` lists every branch with address, ZIP and coordinates.
  - `outOfStock` is the ONLINE shop's stock. `sellOutOfStock: true` means the
    shop sells it anyway, so it is on the shelf as far as anyone can tell;
    meat counter items are almost all "out of stock" online and in the store.
"""

import os
import re
from datetime import UTC, datetime

from chains import close, postcode

# Theirs, and configured rather than assumed: set in the systemd unit
# (https://express.mccaffreys.com, verified 2026-10-10). Unset means the
# adapter is unconfigured — no request is sent and the store says so.
SITE = os.environ.get("THINCART_MCCAFFREYS_SITE", "").strip().rstrip("/")
SEARCH_URL = "/s/{store}/api/b"
STORES_URL = "/api/stores"
PRODUCT_URL = "/s/{store}/i/{id}"
NOT_CONFIGURED = "McCaffrey's prices are not configured (THINCART_MCCAFFREYS_SITE)"

# their abbreviations -> what quantity.parse reads ("FZ" is fluid ounces)
_UNITS = {"FZ": "fl oz", "FLOZ": "fl oz"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pack(size: str) -> str:
    parts = size.strip().split()
    if len(parts) == 2 and parts[1].upper() in _UNITS:
        return f"{parts[0]} {_UNITS[parts[1].upper()]}"
    return size.strip()


# "Family Pack (3 lb. minimum)", "min. 2 lbs": the least a per-pound product
# can be bought in. Its `size` ("1 LB") is only the pricing basis.
_MINIMUM = re.compile(
    r"(\d+(?:\.\d+)?)\s*lbs?\.?\s*min(?:imum)?\b|\bmin(?:imum)?\.?\s*(\d+(?:\.\d+)?)\s*lbs?\b", re.I)


def _per_lb(size: str) -> bool:
    return size.strip().upper() in ("1 LB", "LB")


def _minimum_lb(name: str, size: str) -> float | None:
    """The least weight (lb) a per-pound product is sold in, when its name says
    so. Read as a 1 lb package, a "3 lb minimum" family pack at $2.29/lb would
    beat a real $3.29 pound — but the till charges at least $6.87."""
    if not _per_lb(size):
        return None
    m = _MINIMUM.search(name)
    n = float(m.group(1) or m.group(2)) if m else 0.0
    return n if n > 1 else None


def _place(loc: str) -> dict[str, str]:
    """'2 R' -> aisle 2, right side; 'PRODUCE' -> the Produce department."""
    loc = loc.strip()
    m = re.fullmatch(r"(\d+[A-Z]?)\s*([LR])?", loc.upper())
    if m:
        return {"aisle": m.group(1), "aisle_side": m.group(2) or ""}
    return {"aisle": " ".join(w.capitalize() for w in loc.split()), "aisle_side": ""} if loc else {}


def parse_search(payload: dict, store: str, stamp: str | None = None) -> list[dict] | None:
    """Search hits -> our records, in the shop's ranking. None when the payload
    is not a search answer at all (an error page, a changed API), which is a
    failure to read — never "the shop has none"."""
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("items"), list):
        return None
    stamp = stamp or now_iso()
    out = []
    for h in payload["items"]:
        sku = str(h.get("id") or "").strip()
        name = (h.get("name") or "").strip()
        price, per = h.get("actualPrice"), h.get("actualPriceDivider") or 1
        if not sku or not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        place = _place(str(h.get("location") or ""))
        least = _minimum_lb(name, str(h.get("size") or ""))
        out.append({
            "sku": sku,
            "name": name,
            "brand": (h.get("brand") or "").strip(),
            "sub_brand": "",
            "pack_size": _pack(str(h.get("size") or "")),
            "upc": str(h.get("scanCode") or ""),
            "store_number": store,
            "amount": round(float(price) / float(per), 2),
            # "1 LB" is a per-pound price, weighed at the till: where.compare
            # prices max(wanted, minimum) pro rata rather than in whole pounds
            "by_weight": _per_lb(str(h.get("size") or "")),
            "min_weight_oz": least * 16 if least else 0,
            "unit_price": "",
            "aisle": place.get("aisle", ""), "aisle_side": place.get("aisle_side", ""),
            "section": "", "shelf": "",
            "available": not h.get("outOfStock") or bool(h.get("sellOutOfStock")),
            "source": "mccaffreys",
            "source_url": SITE + PRODUCT_URL.format(store=store, id=sku),
            "fetched_at": stamp,
        })
    return out


def trim_stores(payload: dict) -> list[dict] | None:
    """The branch list, trimmed to what find_branch reads."""
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
        return None
    out = []
    for s in payload["data"]:
        addrs = s.get("addresses") or []
        a = next((x for x in addrs if x.get("location")), addrs[0] if addrs else {})
        if not s.get("id") or not s.get("webStore", True):
            continue
        out.append({"id": str(s["id"]), "name": s.get("name") or "", "lat": s.get("latitude"),
                    "lon": s.get("longitude"), "street": a.get("street1") or "", "city": a.get("city") or "",
                    "state": a.get("state") or "", "postal": str(a.get("postal") or "")[:5]})
    return out or None


def find_branch(stores: list[dict], pin: dict, town: str = "") -> dict | None:
    """The branch a pinned store IS: {"id", "name", "address", "lat", "lon"}, or None.

    The pin's ZIP when exactly one branch has it, else its coordinates within a
    car park's width; for a store with no address, the one branch in the town
    the household typed. Never a nearest-guess: a wrong branch mis-prices
    everything with no visible sign."""
    def branch(s: dict) -> dict:
        return {"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                "address": f"{s['street']}, {s['city']}, {s['state']} {s['postal']}".strip(", ")}

    address = pin.get("address") or ""
    zipc = postcode(address)
    by_zip = [s for s in stores if zipc and s["postal"] == zipc]
    if len(by_zip) == 1:
        return branch(by_zip[0])
    near = [s for s in stores if close(pin.get("lat"), pin.get("lon"), s["lat"], s["lon"])]
    if len(near) == 1:
        return branch(near[0])
    if address or not town:
        return None
    want = re.sub(r"[^a-z0-9]+", "-", town.lower()).strip("-")
    in_town = [s for s in stores if re.sub(r"[^a-z0-9]+", "-", s["city"].lower()).strip("-") == want]
    return branch(in_town[0]) if len(in_town) == 1 else None
