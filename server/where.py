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

import quantity
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


# --- Phase 8: relevance, and the comparison in units we compute ----------------
#
# PLAN.md §2026-10-04. The STORE's own search ranking decides which product
# an item is: its first fitting result. Relevance is only a plain word check
# that this result names the item at all, and sizes of that same product are
# compared with it. Heuristics that tried to judge products on their own
# (product-type word lists, segment counts, ingredient clauses) were removed:
# each fix opened the next edge case on real names.

# Words that say nothing about WHICH product: not required to appear.
_NOT_CONTENT = frozenset(("organic", "fresh", "frozen", "conventional", "the", "and", "of", "with", "a", "an"))
_DELIM = re.compile(r"[,|(\[]")
# Packaging words: two listings differing only in these (and size) are one
# product in two sizes ("…Paper Towels, 12 Rolls, Family Pack" / "…6 Rolls").
_PACK_WORDS = frozenset((
    "family", "value", "pack", "bulk", "size", "bag", "bagged", "box", "jar", "can", "bottle", "carton",
    "tub", "count", "ct", "each", "ea", "multipack", "club", "mega", "jumbo", "big", "large",
))


def _sing(word: str) -> str:
    # "cookies" and "cookie" must fold alike: both to "cooky"
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ie") and len(word) > 4:
        return word[:-2] + "y"
    if word.endswith("oes") or word.endswith("ches") or word.endswith("shes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _percents(text: str) -> set[str]:
    return {f"{float(m):g}%" for m in re.findall(r"(\d+(?:\.\d+)?)\s*%", canonical(text))}


def _words(text: str) -> list[str]:
    # a hyphenated word stays ONE word: "grass-fed" is "grassfed" — also when
    # the store spaced one side of it ("Choose-A- Size"); " - " stays a break
    t = re.sub(r"(?<=[a-z])-\s+(?=[a-z])|(?<=[a-z])\s+-(?=[a-z])", "-", canonical(text))
    return [_sing(w.replace("-", "")) for w in re.findall(r"[a-z]+(?:-[a-z]+)*", t)]


def _covers(want: list[str], got: list[str]) -> bool:
    """Every wanted word is in the product — written apart or joined:
    "oat milk" matches "Oatmilk", "grass fed" matches "Grassfed"."""
    have = set(got) | {a + b for a, b in zip(got, got[1:], strict=False)}
    # "Riced Cauliflower" is cauliflower rice; "Sliced Turkey", turkey slices
    have |= {w[:-1] for w in got if w.endswith("ed")} | {w[:-2] for w in got if w.endswith("ed")}
    for i, w in enumerate(want):
        joined_next = i + 1 < len(want) and w + want[i + 1] in have
        joined_prev = i > 0 and want[i - 1] + w in have
        if w not in have and not joined_next and not joined_prev:
            return False
    return True


def _term_words(term: str) -> list[str]:
    """The item's words, minus a size typed into its name ("salmon 2 lb",
    "milk 64 fl oz"). When that would empty it ("12 eggs") the noun stays."""
    words = _words(quantity.strip_sizes(term))
    return words or _words(term)


def _own_words(name: str, brand: str) -> list[str]:
    """The product's words without its brand — a brand-only segment ("365 by
    Whole Foods Market, …") or a brand written inline at the front."""
    brand_words = set(_words(brand)) | {"by"}
    segs = [s for s in _DELIM.split(canonical(name)) if _words(s)]
    while segs and set(_words(segs[0])) <= brand_words:
        segs = segs[1:]
    # measures go ("16 oz"); counts keep their noun ("12 Eggs" is eggs)
    words = _words(quantity.strip_sizes(" ".join(segs), keep_counts=True))
    lead = _words(brand)
    if lead and words[:len(lead)] == lead:
        words = words[len(lead):]
    return words


def relevant(name: str, term: str, brand: str = "") -> bool:
    """Does this product name the item? Every content word of the item
    appears in the product's own words (any order, joined or apart), and a
    percentage in the item ("2% milk") matches. Nothing more: WHICH of the
    matching products is the item is the store's ranking, not this check."""
    words = _term_words(term)
    want = [w for w in words if w not in _NOT_CONTENT]
    if not want or not canonical(term).isascii():
        return True
    if any(p not in _percents(name) for p in _percents(term)):
        return False
    # "Daisy sour cream": an item naming the product's WHOLE brand has those
    # words satisfied by the brand; an item that is only the brand matches all
    brand_w = _words(brand)
    # matched against ALL the item's words: "Fresh Express spinach" names the
    # whole brand even though "fresh" alone is not a product word
    if brand_w and all(w in words for w in brand_w):
        want = [w for w in want if w not in brand_w]
        if not want:
            return True
    return _covers(want, _own_words(name, brand))


def _identity(rec: dict) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """One product across its sizes: brand + name without sizes and pack
    words — keeping the numbers that are not sizes ("2%" milk is not "1%")."""
    rest = quantity.strip_sizes(rec.get("name") or "")
    words = tuple(w for w in _words(rest) if w not in _PACK_WORDS)
    numbers = tuple(sorted(re.findall(r"\d+(?:\.\d+)?%?", rest)))
    return canonical(rec.get("brand") or ""), words, numbers


def fitting(recs: list[dict], pick_sku: str | None, organic: bool, brand: str,
            term: str) -> tuple[list[tuple[dict, bool]], str]:
    """This store's candidates for the item, and a status.
    A pick is the only candidate at its chain (checked, not trusted).
    Otherwise the store's FIRST fitting result (in stock, priced, organic if
    asked, the preferred brand) that names the item is the product, and other
    sizes of that same product join it."""
    rec, status, exact = choose(recs, pick_sku, organic, brand)
    if pick_sku:
        return ([(rec, True)] if rec else []), status
    fit = [r for r in recs if r.get("available") and r.get("amount") is not None and fits(r, organic, brand)]
    first = next((r for r in fit if relevant(r.get("name") or "", term, r.get("brand") or "")), None)
    if first is None:
        return [], "no_match"
    same = _identity(first)
    # another size must ALSO name the item: "Large Shrimp" shares an identity
    # with "Jumbo Shrimp" once size words go, but it is not jumbo shrimp
    return [(r, False) for r in fit if r is first or (
        _identity(r) == same and relevant(r.get("name") or "", term, r.get("brand") or ""))], "ok"


def compare(cands: list[tuple[dict, dict, bool]], wanted: tuple[str, float] | None,
            liquid: bool = False) -> dict:
    """Rank (store, record, exact) candidates across stores, in ONE dimension:
    the wanted amount's, else the one most candidates can be measured in.
    With a wanted amount the measure is what it costs to buy at least that
    much ("2 × 1 lb = $5.98"), so a huge bag cheaper per pound does not win by
    costing more than the owner meant to spend; without one, the unit price.
    Candidates that cannot be measured in that dimension are listed, unranked."""
    measured = [(s, r, x, quantity.comparable(quantity.quantities(r), liquid)) for s, r, x in cands]
    dim = quantity.choose_dim([m for *_, m in measured], wanted[0] if wanted else None)
    rows = []
    for store, r, exact, qtys in measured:
        row = {"store_id": store["id"], "store": store["name"], "product": r["name"], "brand": r.get("brand", ""),
               "pack_size": r.get("pack_size", ""), "amount": r["amount"], "unit_price": r.get("unit_price", ""),
               "exact": exact, "source": r.get("source", ""), "source_url": r.get("source_url", ""),
               "fetched_at": r.get("fetched_at", ""), "metric": None}
        qty = qtys.get(dim) if dim else None
        if qty and dim:
            row["qty_label"] = quantity.qty_label(qty, dim)
            row["unit_label"] = quantity.unit_label(r["amount"], qty, dim)
            if wanted:
                total, packs = quantity.cost_to_cover(r["amount"], qty, wanted[1])
                row["packs"], row["total"] = packs, total
                row["total_label"] = (f"{packs} × {row['qty_label']} = ${total:.2f}" if packs > 1
                                      else f"{row['qty_label']} = ${total:.2f}")
                row["metric"] = (total, packs * qty - wanted[1])
            else:
                row["metric"] = (r["amount"] / qty, 0)
        rows.append(row)
    ranked = sorted((r for r in rows if r["metric"] is not None), key=lambda r: r["metric"])
    best_per_store: dict[int, dict] = {}
    for r in ranked + [r for r in rows if r["metric"] is None]:
        best_per_store.setdefault(r["store_id"], r)
    for r in rows:
        r.pop("metric", None)
    return {"dim": dim, "cheapest": ranked[0] if ranked else None,
            "comparable": bool(ranked), "quotes": list(best_per_store.values())}
