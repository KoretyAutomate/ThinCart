"""
quantity.py — how much is in a package, and what an amount costs (pure, no I/O).

PLAN.md §Phase 8.2/8.3. The chains' own unit prices cannot be compared with
each other — Wegmans prices paper towels per sq ft, Whole Foods per sheet,
ShopRite each — and Whole Foods never fills in a pack size at all. So the
quantity is worked out here, from the product's own text, into one base unit
per dimension:

    weight → oz   volume → fl oz   each → 1   roll → 1   sheet → 1   area → sq ft

Sources: the pack-size field first, then the product name ("48g Protein per
Pkg" is skipped as a nutrition fact). The store's own unit price only
VALIDATES or fills weight, volume and area — never counts, rolls or sheets
("$2.78/Sheets" on a $7.79 two-roll pack is not a price per sheet). A
dimension stated twice with different amounts ("6ct, 110 CT"), or contradicted
by the unit price by more than 15%, is dropped rather than guessed.
"""

import math
import re
import unicodedata

# unit word -> (dimension, how many base units one of it is)
UNITS: dict[str, tuple[str, float]] = {}
for _words, _dim, _size in (
    (("oz", "ounce", "ounces"), "weight", 1.0),
    (("lb", "lbs", "pound", "pounds"), "weight", 16.0),
    (("g", "gram", "grams"), "weight", 1 / 28.349523125),
    (("kg", "kilogram", "kilograms"), "weight", 1000 / 28.349523125),
    (("fl oz", "fluid ounce", "fluid ounces", "floz"), "volume", 1.0),
    (("gal", "gallon", "gallons"), "volume", 128.0),
    (("qt", "quart", "quarts"), "volume", 32.0),
    (("pt", "pint", "pints"), "volume", 16.0),
    (("l", "liter", "liters", "litre", "litres"), "volume", 33.814),
    (("ml", "milliliter", "milliliters"), "volume", 0.033814),
    (("ct", "count", "each", "ea", "pack", "pk", "pc", "pcs", "piece", "pieces", "egg", "eggs"), "each", 1.0),
    (("roll", "rolls"), "roll", 1.0),
    (("sheet", "sheets"), "sheet", 1.0),
    (("sq ft", "square feet", "square foot", "sqft"), "area", 1.0),
):
    for _w in _words:
        UNITS[_w] = (_dim, _size)

# Longest first, so "fl oz" wins over "oz" and "sq ft" is not read as nothing.
_UNIT_RE = "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
# A whole number only: never the tail of ".5" or "1/2" (Codex review) — those
# are rewritten to decimals by _norm before matching.
_NUM = r"(?<![\d./])(\d+(?:\.\d+)?)"
# "6 Double Plus Rolls": size adjectives may sit between the number and the unit
_ADJ = r"(?:(?:double|triple|mega|plus|family|huge|big|giant|regular|jumbo|select-a-size)\s+){0,3}"
_AMOUNT = re.compile(rf"{_NUM}\s*{_ADJ}({_UNIT_RE})(?![a-z])")
_MULTI = re.compile(rf"(\d+)\s*(?:x|×)\s*{_NUM}\s*({_UNIT_RE})(?![a-z])")
_PER_ROLL = re.compile(r"(\d+)\s*sheets?\s*per\s*roll")
# nutrition facts in names are not package sizes: "48g protein", "5 g sugar"
_NUTRIENT = re.compile(r"\s*(?:of\s+)?(?:protein|fat|sugar|sugars|carb|carbs|fiber|fibre|sodium|calorie)")

# A comparison picks ONE dimension; this order breaks ties between equally
# common ones — physical amounts before counts of things whose size varies.
DIM_ORDER = ("weight", "volume", "area", "sheet", "each", "roll")


def _fraction(m: re.Match) -> str:
    whole = int(m.group(1) or 0)
    return f"{whole + int(m.group(2)) / int(m.group(3)):g}"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)", _fraction, t)   # "1 1/2 lb" -> "1.5 lb"
    t = re.sub(r"(?<![\d])\.(\d)", r"0.\1", t)                    # ".5 lb" -> "0.5 lb"
    t = re.sub(r"(?<=[a-z])\.", "", t)          # "fl. oz." -> "fl oz", keep "0.5"
    t = t.replace("fluid oz", "fl oz").replace("fl.oz", "fl oz")
    return re.sub(r"\s+", " ", t)


def parse(text: str) -> dict[str, float]:
    """Every amount the text states, per dimension, in base units.
    "6 Double Plus Rolls, 103 Sheets Per Roll" -> {"roll": 6, "sheet": 618}.
    A dimension stated twice with different amounts is ambiguous and dropped:
    "Paper Towel 6ct, 110 CT" is 6 rolls of 110 — or 110 of something."""
    t = _norm(text)
    seen: dict[str, set[float]] = {}
    multi_spans = []
    for m in _MULTI.finditer(t):                # "6 x 16 oz" is 96 oz
        dim, size = UNITS[m.group(3)]
        seen.setdefault(dim, set()).add(round(int(m.group(1)) * float(m.group(2)) * size, 3))
        multi_spans.append(m.span())
    for m in _AMOUNT.finditer(t):
        if _NUTRIENT.match(t, m.end()) or any(a <= m.start() < b for a, b in multi_spans):
            continue
        dim, size = UNITS[m.group(2)]
        if dim == "sheet" and _PER_ROLL.match(t, m.start()):
            continue                            # "103 sheets per roll" is not a total
        seen.setdefault(dim, set()).add(round(float(m.group(1)) * size, 3))
    out = {d: next(iter(v)) for d, v in seen.items() if len(v) == 1}
    per = _PER_ROLL.search(t)
    if per and "roll" in out and "sheet" not in out:
        out["sheet"] = int(per.group(1)) * out["roll"]
    return {d: q for d, q in out.items() if q > 0}


def parse_wanted(text: str) -> tuple[str, float] | None:
    """The owner's "how much I want": "2 lb" -> ("weight", 32.0). A bare number
    is a count ("12" -> 12 each). None when it cannot be read — never guessed."""
    t = _norm(text).strip()
    if re.fullmatch(_NUM, t):
        return "each", float(t)
    got = parse(t)
    if len(got) != 1:
        return None
    ((dim, qty),) = got.items()
    return dim, qty


_UNIT_PRICE = re.compile(rf"\$\s*([\d,]*\.?\d+)\s*(?:/|per\s+|\s+)\s*(?:1\s*)?({_UNIT_RE})(?![a-z])")


# Only physical measures: a store's per-sheet/each/roll price is unreliable
# ("$2.78/Sheets" on a two-roll pack), and per-count is often per package.
_TRUSTED_UNIT_DIMS = ("weight", "volume", "area")
DISAGREE = 0.15


def from_unit_price(amount, unit_price: str) -> dict[str, float]:
    """The size a store's own unit price implies: amount ÷ price-per-unit —
    weight, volume and area only."""
    m = _UNIT_PRICE.search(_norm(unit_price))
    if not m or amount is None:
        return {}
    per = float(m.group(1).replace(",", ""))
    dim, size = UNITS[m.group(2)]
    if per <= 0 or dim not in _TRUSTED_UNIT_DIMS:
        return {}
    return {dim: round(float(amount) / per * size, 3)}


def quantities(rec: dict) -> dict[str, float]:
    """What is in this product, per dimension. Pack size first, then the name;
    the store's unit price fills a missing weight/volume/area and vetoes a
    parsed one it contradicts by more than 15% (a rounded unit price — $0.03 a
    sq ft — is allowed to be loose; a different product is not)."""
    out: dict[str, float] = {}
    for got in (parse(rec.get("pack_size") or ""), parse(rec.get("name") or "")):
        for dim, qty in got.items():
            out.setdefault(dim, qty)
    for dim, implied in from_unit_price(rec.get("amount"), rec.get("unit_price") or "").items():
        if dim not in out:
            out[dim] = implied
        elif abs(implied - out[dim]) / out[dim] > DISAGREE and not _rounding_explains(rec, dim, out[dim]):
            del out[dim]
    return out


def _rounding_explains(rec: dict, dim: str, qty: float) -> bool:
    """A unit price shown to the cent is coarse: $15.99 over 627.3 sq ft is
    $0.0255, displayed "$0.03". If the parsed size reproduces the displayed
    unit price once rounded to cents, the disagreement is rounding."""
    m = _UNIT_PRICE.search(_norm(rec.get("unit_price") or ""))
    if not m or rec.get("amount") is None:
        return False
    shown = float(m.group(1).replace(",", ""))
    _, size = UNITS[m.group(2)]
    return round(float(rec["amount"]) / (qty / size), 2) == round(shown, 2)


# Items sold as a liquid. Only for these is "59 oz" read as fluid ounces too —
# 12 oz of honey is not 12 fl oz (Codex review).
LIQUIDS = frozenset((
    "milk", "juice", "water", "oil", "vinegar", "broth", "stock", "cream", "kefir", "drink", "soda",
    "tea", "coffee", "kombucha", "wine", "beer", "lemonade", "creamer", "oatmilk", "seltzer", "sauce",
))


def is_liquid(term: str) -> bool:
    return any(w in LIQUIDS for w in re.findall(r"[a-z]+", (term or "").lower()))


def comparable(qtys: dict[str, float], liquid: bool = False) -> dict[str, float]:
    """For a LIQUID item, stores write "oz" ("Organic Valley 2% Milk, 59 oz") as
    often as "fl oz", so a product stating only one of weight/volume is also
    measured in the other (1 oz ≈ 1 fl oz for water-like liquids). Without it,
    milk in fl oz at one store and oz at another would never compare. For
    anything else weight and volume stay apart."""
    out = dict(qtys)
    if not liquid:
        return out
    if "weight" in out and "volume" not in out:
        out["volume"] = out["weight"]
    elif "volume" in out and "weight" not in out:
        out["weight"] = out["volume"]
    return out


def choose_dim(candidates: list[dict[str, float]], wanted: str | None = None) -> str | None:
    """The one dimension a comparison is made in: the wanted amount's, else the
    one most candidates can be measured in (ties by DIM_ORDER)."""
    if wanted:
        return wanted
    counts = {d: sum(1 for c in candidates if d in c) for d in DIM_ORDER}
    best = max(DIM_ORDER, key=lambda d: (counts[d], -DIM_ORDER.index(d)))
    return best if counts[best] else None


SLACK = 0.05  # a 15.9 oz jar covers "1 lb"; a 15 oz one does not


def packs_needed(wanted: float, qty: float) -> int:
    return max(1, math.ceil(wanted / qty - SLACK))


def cost_to_cover(amount: float, qty: float, wanted: float) -> tuple[float, int]:
    """(total, packs) to buy at least `wanted` in packs of `qty` at `amount`."""
    n = packs_needed(wanted, qty)
    return round(n * amount, 2), n


_LABEL = {"weight": "oz", "volume": "fl oz", "area": "sq ft", "each": "each", "roll": "roll"}


def unit_label(amount: float, qty: float, dim: str) -> str:
    """"$0.21/oz", "$1.05/100 sheets" — per 100 for sheets, a cent each is noise."""
    if dim == "sheet":
        return f"${amount / qty * 100:.2f}/100 sheets"
    per = amount / qty
    return f"${per:.2f}/{_LABEL[dim]}" if per >= 0.01 else f"${per * 100:.2f}/100 {_LABEL[dim]}"


def qty_label(qty: float, dim: str) -> str:
    if dim == "weight":
        return f"{qty / 16:g} lb" if qty >= 16 and qty % 16 == 0 else f"{qty:g} oz"
    if dim == "volume":
        return f"{qty / 128:g} gal" if qty >= 128 and qty % 128 == 0 else f"{qty:g} fl oz"
    return f"{qty:g} {'sq ft' if dim == 'area' else dim + ('s' if dim in ('roll', 'sheet') and qty != 1 else '')}"
