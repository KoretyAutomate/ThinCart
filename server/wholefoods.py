"""
wholefoods.py — reading Whole Foods' pages. No network: every function here
turns a payload into our shape, or a string into another string.

Same seam as wegmans.py: lookup.py owns the outbound policy and makes the
requests; this module knows what the answers mean. Kept pure so the tests can
pin the parsing against RECORDED pages without touching the network.

What was learned, and what the shape below is built on (PLAN.md §2026-09-07):

  - The site has two faces. With no store selected it is a delivery front with
    no shelf data anywhere. With a store selected it serves `/grocery/...`, and
    that is where prices and shelf positions live. Selection is a cookie,
    `wfm_store_d8`: base64 JSON naming the branch. It can be BUILT rather than
    negotiated — send it and the page comes back rendered for that store.
  - The branch's number is on its store page as `"storeCode":"10187"`, next to
    `"wholeFoodsMarketFolder":"princeton"`, the slug the page is keyed by.
  - Search results arrive server-rendered inside the page's `__NEXT_DATA__`
    island, under `props.pageProps.productsInfo`, each with the store's price.
    They carry NO shelf position.
  - The product page's `__NEXT_DATA__` has `props.pageProps.productLocation`:
    a department ("Dairy"), or null for produce and other unplaced items. The
    page resolves from the ASIN alone: `/grocery/product/B000O6EFHO`.
"""

import base64
import json
import os
import re
from datetime import UTC, datetime
from urllib.parse import quote

from chains import address_town_state

# Theirs, not ours, and overridable from the systemd unit like the Wegmans
# endpoints in lookup.py — a redesign or a proxy is a config change, not a
# code change. The defaults are what was verified on 2026-09-07.
SITE = os.environ.get("THINCART_WHOLEFOODS_SITE", "https://www.wholefoodsmarket.com").strip().rstrip("/")
STORE_URL = SITE + "/stores/{slug}"
SEARCH_URL = SITE + "/grocery/search?k={term}"
PRODUCT_URL = SITE + "/grocery/product/{asin}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def wholefoods_slug(address: str) -> str:
    """'princeton' — the store page is keyed by town alone. Empty when the
    address does not parse; never a guessed town."""
    town, _ = address_town_state(address)
    return town


def parse_store(html: str) -> dict | None:
    """The branch as the site identifies it, off its store page — or None when
    the page cannot be read. A 200 with no store code is a failure to READ (a
    redesign, an interstitial), not a branch that does not exist, and the two
    must never be cached alike."""
    m = re.search(r'"storeCode"\s*:\s*"(\d+)"', html)
    if not m:
        return None
    code = m.group(1)
    folder = re.search(r'"wholeFoodsMarketFolder"\s*:\s*"([^"]+)"', html)
    # The address and geocode sit just before the code on the page we recorded;
    # the search is windowed so a different branch mentioned elsewhere cannot
    # leak in. The ZIP and coordinates are what let the caller check that the
    # page it landed on is the branch that was pinned — /stores/<town> is one
    # store, and a town can have several.
    window = html[max(0, m.start() - 2000): m.start()]
    city = re.search(r'"city"\s*:\s*"([^"]+)"', window)
    state = re.search(r'"state"\s*:\s*"([A-Z]{2})"', window)
    zipc = re.search(r'"postalCode"\s*:\s*"(\d{5})', window)
    geo = re.findall(r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*"longitude"\s*:\s*(-?\d+(?:\.\d+)?)', window)
    return {
        "code": code,
        "folder": folder.group(1) if folder else "",
        "name": city.group(1) if city else "",
        "state": state.group(1) if state else "",
        "postcode": zipc.group(1) if zipc else "",
        "lat": float(geo[-1][0]) if geo else None,
        "lon": float(geo[-1][1]) if geo else None,
    }


def store_cookie(code: str, folder: str = "", name: str = "", state: str = "") -> str:
    """The `wfm_store_d8` value that selects a branch. The id is what the site
    reads; the rest is what its own cookie carries and is filled where known."""
    payload = {"id": str(code), "name": name, "tlc": "", "path": folder, "state": state,
               "store_nid": ""}
    return quote(base64.b64encode(json.dumps(payload).encode()).decode())


def next_data(html: str) -> dict | None:
    """The page's `__NEXT_DATA__` island, or None when there is none — which is
    what a bot wall or an empty shell looks like, and is a read failure."""
    i = html.find('id="__NEXT_DATA__"')
    if i < 0:
        return None
    start = html.find(">", i) + 1
    end = html.find("</script>", start)
    if start <= 0 or end < 0:
        return None
    try:
        return json.loads(html[start:end])
    except ValueError:
        return None


def parse_search(html: str, store_code: str, stamp: str | None = None) -> list[dict] | None:
    """Search page -> our records, in the site's ranking. None when the page
    could not be read; [] when it genuinely lists nothing.

    Records carry the store's price and no aisle: that is on the product page,
    and lookup.py fills it for the products that matter (see `parse_location`).
    A hit without a price is dropped — with no aisle either, it would answer
    neither question and render as "free, location unknown".
    """
    nd = next_data(html)
    if nd is None:
        return None
    try:
        products = nd["props"]["pageProps"]["productsInfo"]
    except (KeyError, TypeError):
        return None
    stamp = stamp or now_iso()
    out = []
    for p in products or []:
        asin = str(p.get("asin") or "").strip()
        name = (p.get("name") or "").strip()
        offer = p.get("offerDetails") or {}
        amount = (offer.get("price") or {}).get("priceAmount")
        if not asin or not name or amount is None:
            continue
        unit = offer.get("unitPrice") or {}
        unit_price = ""
        if unit.get("priceAmount") is not None and unit.get("baseUnit"):
            unit_price = f"${float(unit['priceAmount']):.2f}/{unit['baseUnit']}"
        out.append({
            "sku": asin,
            "name": name,
            "brand": (p.get("brandName") or "").strip(),
            "sub_brand": "",
            "pack_size": "",
            "upc": "",
            "store_number": str(store_code),
            "amount": float(amount),
            "unit_price": unit_price,
            "aisle": "", "aisle_side": "", "section": "", "shelf": "",
            "available": (p.get("availability") or "").upper() == "IN_STOCK",
            "source": "wholefoods",
            "source_url": PRODUCT_URL.format(asin=asin),
            "fetched_at": stamp,
        })
    return out


def parse_location(html: str) -> str | None:
    """The department a product page places the item in ("Dairy"), "" when the
    page says it has none (produce comes back null), or None when the page
    could not be read at all. The last two must stay apart: one is an answer
    about the shop, the other is a failure to ask."""
    nd = next_data(html)
    if nd is None:
        return None
    try:
        loc = nd["props"]["pageProps"]["productLocation"]
    except (KeyError, TypeError):
        return None
    return (str(loc).strip() if loc else "")
