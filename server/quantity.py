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
_NUM = r"(?<![\d.,/])(\d+(?:\.\d+)?)"
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
    """"1 1/2" -> "1.5". A zero denominator is not a number: it becomes a
    marker nothing matches, so the amount reads as "not understood" — never an
    exception (a saved "1/0 lb" would otherwise break every state read)."""
    whole, num, den = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return f"{whole + num / den:g}" if den else " ?? "


def _vulgar(text: str) -> str:
    """"1½" must read 1 1/2, but NFKC alone turns it into "11⁄2" (eleven
    halves). So each fraction character becomes " n/d" BEFORE normalizing."""
    return "".join(
        " " + unicodedata.normalize("NFKC", ch).replace("\u2044", "/") + " "
        if unicodedata.name(ch, "").startswith("VULGAR FRACTION") else ch
        for ch in text
    )


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", _vulgar(text or "")).lower()
    t = t.replace("\u2044", "/")
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)                 # "1,100 sheets" -> "1100 sheets"
    t = re.sub(r"(?:(\d+)(?:\s+|\s*-\s*))?(\d+)\s*/\s*(\d+)", _fraction, t)  # "1 1/2", "1-1/2" -> "1.5"
    t = re.sub(r"(?<![\d])\.(\d)", r"0.\1", t)                    # ".5 lb" -> "0.5 lb"
    t = re.sub(r"(?<=[a-z])\.", "", t)          # "fl. oz." -> "fl oz", keep "0.5"
    t = t.replace("fluid oz", "fl oz").replace("fl.oz", "fl oz")
    return re.sub(r"\s+", " ", t)


# Size words with no number: "Milk, Half Gallon", "Cream, Quart".
_WORD_SIZES = (
    (re.compile(r"\bhalf[\s-]+gallon\b"), "volume", 64.0),
    (re.compile(r"(?<![\d.\s])\s*\bgallon\b|^gallon\b"), "volume", 128.0),
    (re.compile(r"(?<![\d.])\s*\bquart\b"), "volume", 32.0),
    (re.compile(r"(?<![\d.])\s*\bpint\b"), "volume", 16.0),
)
# "(Pack of 6)", "6 pack", "6-pack", "6 cans", "12 bottles": a count of the
# stated size, not a size of its own.
_PACK_OF = re.compile(r"\bpack\s+of\s+(\d+)\b|(?<![\d.])(\d+)\s*-?\s*(?:pack|pk|cans|bottles|cartons|jars|boxes)\b")
# "12 oz (340 g)" is one package stated in two units; values this close are
# the same size, rounded differently.
SAME = 0.03


def _one_value(values: list[float]) -> float | None:
    """The single size a dimension was stated as — as first stated ("12 oz
    (340 g)" is 12 oz) — or None if it was stated as genuinely different sizes."""
    values = [v for v in values if v > 0]   # "0 lb" is not a size — and must never divide
    if not values:
        return None
    lo, hi = min(values), max(values)
    return values[0] if (hi - lo) / hi <= SAME else None


def pack_count(text: str) -> int:
    """N in "6 x 12 fl oz", "(Pack of N)", "N pack", "N cans" — 1 when not stated."""
    t = _norm(text)
    multi = _MULTI.search(t)
    if multi:
        return int(multi.group(1))
    m = _PACK_OF.search(t)
    return int(m.group(1) or m.group(2)) if m else 1


def _word_sizes(t: str) -> dict[str, float]:
    for rx, dim, qty in _WORD_SIZES:
        if rx.search(t):
            return {dim: qty}
    return {}


def parse(text: str) -> dict[str, float]:
    """Every amount the text states, per dimension, in base units.
    "6 Double Plus Rolls, 103 Sheets Per Roll" -> {"roll": 6, "sheet": 618};
    "Seltzer, 12 fl oz (Pack of 6)" -> 72 fl oz; "Milk, Half Gallon" -> 64.
    A dimension stated as genuinely different sizes is ambiguous and dropped:
    "Paper Towel 6ct, 110 CT" is 6 rolls of 110 — or 110 of something."""
    t = _norm(text)
    seen: dict[str, list[float]] = {}
    multi_spans = []
    for m in _MULTI.finditer(t):                # "6 x 16 oz" is 96 oz
        dim, size = UNITS[m.group(3)]
        seen.setdefault(dim, []).append(round(int(m.group(1)) * float(m.group(2)) * size, 3))
        multi_spans.append(m.span())
    for m in _AMOUNT.finditer(t):
        if _NUTRIENT.match(t, m.end()) or any(a <= m.start() < b for a, b in multi_spans):
            continue
        dim, size = UNITS[m.group(2)]
        if dim == "sheet" and _PER_ROLL.match(t, m.start()):
            continue                            # "103 sheets per roll" is not a total
        seen.setdefault(dim, []).append(round(float(m.group(1)) * size, 3))
    out = {d: v for d, v in ((d, _one_value(vals)) for d, vals in seen.items()) if v is not None}
    for dim, qty in _word_sizes(t).items():
        if dim not in seen:          # never revive a size the numbers ruled out ("0 quart")
            out[dim] = qty
    pack = _PACK_OF.search(t)
    n = int(pack.group(1) or pack.group(2)) if pack else 1
    if n > 1 and not multi_spans:               # "6 x 12 oz" already counted the cans
        for dim in ("weight", "volume"):
            if dim in out:
                out[dim] = round(out[dim] * n, 3)
    per = _PER_ROLL.search(t)
    if per and "roll" in out and "sheet" not in out:
        out["sheet"] = int(per.group(1)) * out["roll"]
    return {d: q for d, q in out.items() if q > 0}


def parse_wanted(text: str) -> tuple[str, float] | None:
    """The owner's "how much I want": "2 lb" -> ("weight", 32.0). A bare number
    is a count ("12" -> 12 each). None when it cannot be read — never guessed."""
    t = _norm(text).strip()
    if re.fullmatch(_NUM, t):
        return ("each", float(t)) if float(t) > 0 else None
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


# Common package sizes, oz / fl oz. A size implied by a unit price shown to
# the cent is only known to a range — $3.09 at "$0.05/fl oz" is 56–69 fl oz —
# and the half gallon (64) in that range is the package, not 61.8.
_STANDARD = {
    "weight": (1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 28, 32, 40, 48, 64, 80, 96, 128, 160, 320),
    "volume": (8, 10, 12, 16, 20, 24, 25.4, 32, 33.8, 46, 48, 52, 59, 64, 67.6, 89, 96, 101.4, 128, 192, 256),
}


def from_unit_price(amount, unit_price: str) -> dict[str, float]:
    """The size a store's own unit price implies: amount ÷ price-per-unit —
    weight, volume and area only — snapped to a standard size when the cent
    rounding of the unit price allows exactly one."""
    m = _UNIT_PRICE.search(_norm(unit_price))
    if not m or amount is None:
        return {}
    per = float(m.group(1).replace(",", ""))
    dim, size = UNITS[m.group(2)]
    if per <= 0 or dim not in _TRUSTED_UNIT_DIMS:
        return {}
    amount = float(amount)
    estimate = amount / per * size
    lo, hi = amount / (per + 0.005) * size, amount / max(per - 0.005, 1e-9) * size
    fits = [s for s in _STANDARD.get(dim, ()) if lo <= s <= hi]
    if len(fits) == 1:
        return {dim: float(fits[0])}
    if len(fits) > 1:   # several standard sizes fit: take the nearest to the estimate
        return {dim: float(min(fits, key=lambda s: abs(s - estimate)))}
    return {dim: round(estimate, 3)}


def _unit_price_range(rec: dict, dim: str) -> tuple[float, float] | None:
    """The sizes a cent-rounded unit price allows, in base units: $4 at
    "$0.04/fl oz" is anything from 89 to 114 fl oz."""
    m = _UNIT_PRICE.search(_norm(rec.get("unit_price") or ""))
    if not m or rec.get("amount") is None or UNITS[m.group(2)][0] != dim or dim not in _TRUSTED_UNIT_DIMS:
        return None
    per, size, amount = float(m.group(1).replace(",", "")), UNITS[m.group(2)][1], float(rec["amount"])
    return amount / (per + 0.005) * size, amount / max(per - 0.005, 1e-9) * size


def quantities(rec: dict) -> dict[str, float]:
    """What is in this product, per dimension. Pack size first, then the name;
    the store's unit price fills a missing weight/volume/area and vetoes a
    parsed one it contradicts by more than 15% (a rounded unit price — $0.03 a
    sq ft — is allowed to be loose; a different product is not)."""
    pack_q, name = parse(rec.get("pack_size") or ""), rec.get("name") or ""
    name_q = parse(name)
    out: dict[str, float] = dict(pack_q)
    for dim, qty in name_q.items():
        out.setdefault(dim, qty)
    settled: set[str] = set()   # decided by the pack-count reconciliation below
    n = pack_count(name)
    for dim in ("weight", "volume"):
        if n <= 1 or dim not in pack_q:
            continue
        if dim in name_q:
            # pack size "12 fl oz" (one can) + name "…, 8 pack" (96 fl oz): when
            # the name's total is the pack size × N, it is the package
            if abs(pack_q[dim] * n - name_q[dim]) <= SAME * name_q[dim]:
                out[dim] = name_q[dim]
            continue
        # "Sparkling Water (8 cans)" with pack size "12 fl oz": per can, or the
        # whole pack? The unit price decides when its rounding range holds one
        # reading and not the other; otherwise the size is unknown — unranked.
        rng = _unit_price_range(rec, dim)
        whole, one = pack_q[dim] * n, pack_q[dim]
        holds = [v for v in (whole, one) if rng and rng[0] <= v <= rng[1]]
        if len(holds) == 1:
            out[dim] = holds[0]
        else:
            del out[dim]
        settled.add(dim)
    for dim, by_unit in from_unit_price(rec.get("amount"), rec.get("unit_price") or "").items():
        if dim in settled:      # the unit price already had its say there
            continue
        if dim not in out:
            out[dim] = by_unit
        elif abs(by_unit - out[dim]) / out[dim] > DISAGREE and not _rounding_explains(rec, dim, out[dim]):
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


# Items that are ONLY ever sold as a liquid. Only for these is "59 oz" read as
# fluid ounces too — 12 oz of honey is not 12 fl oz. Words that name both a
# dry and a liquid product are left out on purpose: coffee (ground vs cold
# brew), tea (leaves vs bottled), cream (sour vs heavy), sauce (Codex review).
LIQUIDS = frozenset((
    "milk", "juice", "water", "oil", "vinegar", "broth", "stock", "kefir", "drink", "soda",
    "kombucha", "wine", "beer", "lemonade", "creamer", "oatmilk", "seltzer",
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
