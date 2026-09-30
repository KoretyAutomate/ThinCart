"""
where.py — which store is cheapest for an item (pure, no I/O).

PLAN.md §Phase 7C and its review deltas. The rules that make a "cheapest"
claim honest:

- A price is compared per UNIT, never per package. 6 oz for $3 is not cheaper
  than 32 oz for $5. Unit prices are normalised to one dimension — weight to
  $/oz, volume to $/fl oz, count to $/each — and a store is named cheapest
  only when every candidate has a unit price in the SAME dimension. Otherwise
  the quotes are shown and no winner is claimed.
- Organic and brand are read from the product's own text (name, brand,
  sub-brand); nothing is inferred.
- A product the household picked is the candidate at its chain — but only if
  it is in stock and still matches the item's preferences. A pick that no
  longer matches is a `conflict`, reported and excluded, never quietly used.
"""

import re

from db import canonical

# unit word (dots removed, lower-case) -> (dimension, how many base units it is)
_UNITS: dict[str, tuple[str, float]] = {
    "oz": ("weight", 1.0), "ounce": ("weight", 1.0), "ounces": ("weight", 1.0),
    "lb": ("weight", 16.0), "lbs": ("weight", 16.0), "pound": ("weight", 16.0),
    "g": ("weight", 1 / 28.349523125), "gram": ("weight", 1 / 28.349523125),
    "kg": ("weight", 1000 / 28.349523125),
    "fl oz": ("volume", 1.0), "fluid ounce": ("volume", 1.0), "fluid ounces": ("volume", 1.0),
    "gallon": ("volume", 128.0), "gal": ("volume", 128.0), "quart": ("volume", 32.0),
    "qt": ("volume", 32.0), "pint": ("volume", 16.0), "pt": ("volume", 16.0),
    "l": ("volume", 33.814), "liter": ("volume", 33.814), "ml": ("volume", 0.033814),
    "each": ("count", 1.0), "ea": ("count", 1.0), "count": ("count", 1.0), "ct": ("count", 1.0),
}

_UNIT_PRICE = re.compile(r"\$\s*([\d,]*\.?\d+)\s*(?:/|per\s+|\s+)\s*([a-z][a-z ]*)")

ORGANIC_WORDS = ("organic", "オーガニック", "有機")


def unit_value(unit_price: str) -> tuple[str, float] | None:
    """'$0.19/ounce' -> ('weight', 0.19); '$2.40/lb' -> ('weight', 0.15).
    None when there is no unit price or its unit is not one we can compare
    (e.g. '$0.02/sq. ft.')."""
    # drop abbreviation dots ("fl. oz." -> "fl oz") but not the decimal point
    text = re.sub(r"(?<=[a-z])\.", "", (unit_price or "").lower())
    m = _UNIT_PRICE.search(text)
    if not m:
        return None
    unit = " ".join(m.group(2).split())
    if unit not in _UNITS:
        return None
    dim, size = _UNITS[unit]
    return dim, float(m.group(1).replace(",", "")) / size


def _texts(rec: dict) -> tuple[str, str, str]:
    return canonical(rec.get("name") or ""), canonical(rec.get("brand") or ""), canonical(rec.get("sub_brand") or "")


def is_organic(rec: dict) -> bool:
    name, brand, sub = _texts(rec)
    return any(w in t for w in ORGANIC_WORDS for t in (name, brand, sub))


def brand_ok(rec: dict, want: str) -> bool:
    """The preferred brand IS the product's brand or sub-brand, or appears in
    its name as whole words — "Ann" must not match "Annie's"."""
    want = canonical(want)
    if not want:
        return True
    name, brand, sub = _texts(rec)
    if want in (brand, sub):
        return True
    return re.search(rf"(?<![\w']){re.escape(want)}(?![\w'])", name) is not None


def fits(rec: dict, organic: bool, brand: str) -> bool:
    return (not organic or is_organic(rec)) and brand_ok(rec, brand)


def choose(recs: list[dict], pick_sku: str | None, organic: bool, brand: str) -> tuple[dict | None, str, bool]:
    """(record, status, exact) for one item at one store.

    status: 'ok' — a candidate; 'conflict' — the picked product no longer fits
    the item's preferences or is out of stock; 'pick_missing' — the picked
    product was not in the results; 'no_match' — nothing here fits.
    """
    if pick_sku:
        # Once the household has said which product they buy here, a different
        # one is not its price: the pick is the only candidate at this chain.
        for r in recs:
            if r.get("sku") == pick_sku:
                if r.get("available") and r.get("amount") is not None and fits(r, organic, brand):
                    return r, "ok", True
                return None, "conflict", True
        return None, "pick_missing", True
    for r in recs:
        if r.get("available") and r.get("amount") is not None and fits(r, organic, brand):
            return r, "ok", False
    return None, "no_match", False


def _per_package(unit: tuple[str, float], quote: dict) -> bool:
    """A count unit price equal to the shelf price is a price per PACKAGE —
    Whole Foods quotes a 64 oz milk as "$6.29/count" at $6.29 — and two
    packages of different sizes are not comparable by it. "$0.30/each" on a
    $1.49 bag of five is a real per-item price and stays comparable."""
    dim, value = unit
    amount = quote.get("amount")
    return dim == "count" and amount is not None and abs(value - float(amount)) < 0.005


def rank(quotes: list[dict]) -> tuple[dict | None, bool]:
    """(cheapest, comparable). Cheapest per unit, and only when every quote has
    a unit price in one dimension — otherwise there is no honest winner."""
    if not quotes:
        return None, False
    valued: list[tuple[str, float, dict]] = []
    for q in quotes:
        u = unit_value(q.get("unit_price", ""))
        if u is None or _per_package(u, q):
            return None, False
        valued.append((u[0], u[1], q))
    if len({dim for dim, _, _ in valued}) != 1:
        return None, False
    return min(valued, key=lambda v: v[1])[2], True
