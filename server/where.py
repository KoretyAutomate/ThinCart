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

# Words that turn the item into a DIFFERENT product when they follow it in a
# product name: "Lime Juice" is not lime, "Egg White Wraps" not egg whites,
# "Cauliflower Rice" not cauliflower. Drawn from the false positives in the
# cached search results (PLAN.md Phase 8, review delta 6). Ignored when the
# item's own name contains the word ("lime juice" may match "Lime Juice").
_COMPOUND_WORDS = (
    "juice", "drink", "drinks", "water", "soda", "tea", "coffee", "kombucha", "smoothie", "shake",
    "chicken", "beef", "pork", "turkey", "cutlet", "cutlets", "breast", "ravioli", "wrap", "wraps",
    "bite", "bites", "blend", "waffle", "waffles", "honey", "syrup", "chip", "chips", "tortilla",
    "tortillas", "yogurt", "bake", "bowl", "bowls", "burrito", "sauce", "dressing", "paste",
    "flour", "cake", "cakes", "cracker", "crackers", "cereal", "vinegar", "wine", "soup", "broth",
    "probiotic", "oil", "butter", "bread", "bar", "bars", "cookie", "cookies", "candy",
    "chocolate", "cream", "pie", "mix", "seasoning", "spread", "dip", "hummus", "salsa",
    "marinade", "marinated", "kit", "rice", "noodle", "noodles", "sprouts", "pudding", "jam",
    "relish", "pickle", "pickles", "vinaigrette", "aioli", "mayo", "mayonnaise", "ketchup", "mustard",
    "frosting", "icing", "glaze", "filling",
    "jelly", "popsicle", "gummies", "granola", "muffin", "muffins", "pancake", "pancakes", "pizza",
    "sandwich", "dumpling", "dumplings",
)

# Words that say nothing about WHICH product: not required to appear.
_NOT_CONTENT = frozenset(("organic", "fresh", "frozen", "conventional", "the", "and", "of", "with", "a", "an"))
_DELIM = re.compile(r"[,|(\[]")
_INGREDIENT_INTRO = frozenset(("with", "made", "in", "featuring", "plus", "infused"))


def _sing(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") or word.endswith("ches") or word.endswith("shes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _percents(text: str) -> set[str]:
    return {f"{float(m):g}%" for m in re.findall(r"(\d+(?:\.\d+)?)\s*%", canonical(text))}


# folded exactly as product words are, or "cookies" (-> "cooky") slips past it
COMPOUND = frozenset(_sing(w) for w in _COMPOUND_WORDS)


def _words(text: str) -> list[str]:
    # a hyphenated word stays ONE word: "Stir-In Paste" has no "in" that could
    # read as an ingredient clause, and "grass-fed" is "grassfed" (Codex review)
    return [_sing(w.replace("-", "")) for w in re.findall(r"[a-z]+(?:-[a-z]+)*", canonical(text))]


def _leading_phrase(name: str, brand: str) -> str:
    """The product's own phrase: the first comma/bar-separated segment that is
    not just its brand — "365 by Whole Foods Market, Tofu Firm Organic" leads
    with the tofu, not the store brand."""
    brand_words = set(_words(brand)) | {"by"}
    for seg in _DELIM.split(canonical(name)):
        words = _words(seg)
        if words and not set(words) <= brand_words:
            return seg
    return ""


def _covers(want: list[str], got: list[str]) -> bool:
    """Every wanted word is in the product — written apart or joined:
    "oat milk" matches "Oatmilk", "oatmilk" matches "Oat Milk", "grass fed"
    matches "Grassfed" (Codex review: real Wegmans oat milks were rejected)."""
    have = set(got) | {a + b for a, b in zip(got, got[1:], strict=False)}
    for i, w in enumerate(want):
        joined_next = i + 1 < len(want) and w + want[i + 1] in have
        joined_prev = i > 0 and want[i - 1] + w in have
        if w not in have and not joined_next and not joined_prev:
            return False
    return True


def _positions(lead: list[str], want: list[str]) -> list[int]:
    """Where in the product's phrase the item is named, either spelling."""
    pairs = {a + b for a, b in zip(want, want[1:], strict=False)}
    return [i for i, t in enumerate(lead)
            if t in want or t in pairs or (i + 1 < len(lead) and t + lead[i + 1] in want)]


def _term_words(term: str) -> list[str]:
    """The item's words, minus a size typed into its name: "salmon 2 lb" and
    "milk 64 fl oz" are salmon and milk. Only whole size PHRASES go — "eggs"
    alone is the product, not a count (Codex review)."""
    return _words(quantity.strip_sizes(term))


def _core_words(name: str, brand: str) -> list[str]:
    """The words that describe THIS product: the name without its brand —
    a brand-only segment ("365 by Whole Foods Market, …") or a brand written
    inline at the front ("Pumpkin Tree Strawberry … Puree") — cut at the
    first ingredient clause ("… with Chicken & Pumpkin"). The item's words
    must be found here — not in the brand, not among the ingredients."""
    brand_words = set(_words(brand)) | {"by"}
    segs = [s for s in _DELIM.split(canonical(name)) if _words(s)]
    while segs and set(_words(segs[0])) <= brand_words:
        segs = segs[1:]
    # Descriptive segments are few ("SoyBoy Tofu, Organic, Extra Firm";
    # "Cheese Slices, Non-Smoked, Provolone"); ingredient and flavour lists are
    # long ("Baby Puree, Organic, White Bean, Pumpkin, Apple"). A name of up to
    # three segments is all description; a longer one is read only to its
    # second, so a flavour list cannot name the item (Codex review).
    words = _words(" ".join(segs if len(segs) <= 3 else segs[:2]))
    lead_brand = _words(brand)
    if lead_brand and words[:len(lead_brand)] == lead_brand:
        words = words[len(lead_brand):]
    for i, w in enumerate(words):
        if w in _INGREDIENT_INTRO:
            return words[:i]
    return words


def relevant(name: str, term: str, brand: str = "") -> bool:
    """Is this product the item, rather than something made from it?
    Every content word of the item appears (any order — Whole Foods writes
    "Tofu Firm Organic"), and no COMPOUND word follows it before the first
    comma. An item with no English name cannot be checked, and passes."""
    want = [w for w in _term_words(term) if w not in _NOT_CONTENT]
    # decided on the NORMALIZED term: "２％ milk" is ASCII once folded
    if not want or not canonical(term).isascii():
        return True
    # numbers that name the product ("2% milk") must match: words alone
    # would let a cheaper 1% win (Codex review)
    if any(p not in _percents(name) for p in _percents(term)):
        return False
    head = _leading_phrase(name, brand)
    # "Daisy sour cream": the item names the product's WHOLE brand, so those
    # words are satisfied by the brand. A brand only partly in the item
    # ("Pumpkin Tree" for pumpkin puree) satisfies nothing (Codex review).
    brand_w = _words(brand)
    if brand_w and all(w in want for w in brand_w):
        want = [w for w in want if w not in brand_w] or want
    if not _covers(want, _core_words(name, brand)):
        return False
    # a product-type word AFTER any of the item's words in the leading phrase
    # makes it another product: "Rice Cakes, Brown Rice" is cakes. One BEFORE
    # them describes it: "Honey Roasted Peanuts" are peanuts.
    lead = _words(head)
    # "Body Lotion with Virgin Coconut Oil": what follows "with" lists
    # ingredients — it neither names the item nor changes what the product is
    for i, w in enumerate(lead):
        if w in _INGREDIENT_INTRO:
            lead = lead[:i]
            break
    at = _positions(lead, want)
    if not at:
        # the item is only mentioned outside the product's own phrase — an
        # ingredient ("Lemonade, made with real lemon"), not the item
        return False
    after = lead[at[0] + 1:]
    return not any(w in COMPOUND and w not in want for w in after)


def fitting(recs: list[dict], pick_sku: str | None, organic: bool, brand: str,
            term: str) -> tuple[list[tuple[dict, bool]], str]:
    """Every candidate this store offers for the item, and a status.
    A pick is the only candidate at its chain (checked, not trusted);
    otherwise each in-stock, priced, organic-if-asked, brand-matching AND
    relevant result is one."""
    rec, status, exact = choose(recs, pick_sku, organic, brand)
    if pick_sku:
        return ([(rec, True)] if rec else []), status
    out = [(r, False) for r in recs
           if r.get("available") and r.get("amount") is not None
           and fits(r, organic, brand) and relevant(r.get("name") or "", term, r.get("brand") or "")]
    return out, ("ok" if out else "no_match")


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
