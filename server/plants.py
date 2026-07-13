"""
plants.py — the canonical plant vocabulary + the weighted plant-point counter.

Two jobs, both deterministic and LLM-free (PLAN.md: the counter must keep working
with the DGX down):

1. NORMALIZE.  The LLM emits plant tokens; left unchecked it drifts
   (`ピーマン`→"capsicum" but `bell pepper bag`→"pepper"; `レモン`+`Lime`→"citrus").
   Drift causes two distinct failures — the same species under two tokens
   DOUBLE-COUNTS in the weekly union, and one token for two species (bell pepper
   the vegetable vs black pepper the spice, both "pepper") COLLIDES and hides one
   of them. `normalize()` folds every known synonym onto one canonical token and
   resolves the genuinely ambiguous bare tokens using the item's own name.

   The unit of the vocabulary is the CULINARY TAXON: one token per species,
   except where a single species is eaten in two unrelated roles (Capsicum annuum
   is both a vegetable and a spice) — then the roles split, because they carry
   different plant-point weights. Colour/cultivar never splits a token:
   green/red/yellow bell pepper are all `bell pepper`, white/red onion are both
   `onion` (see WEIGHTS docstring for the sourcing on that choice).

2. WEIGH.  Plant points are not all 1.0 — see WEIGHTS.
"""
import re

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------
# The "30 plants a week" number comes from the American Gut Project
# (McDonald et al. 2018, mSystems). The STUDY itself is a plain self-reported
# count: no points, no fractions, no exclusions — its own survey wording tells
# you to count every plant in a can of soup separately.
#
# The FRACTIONAL system is Dr Megan Rossi's ("plant points", Eat More Live Well),
# NOT ZOE's and not the study's: herbs & spices, garlic, extra-virgin olive oil,
# tea and coffee each score 1/4; vegetable stock 1/2; everything in the "Super
# Six" (veg, fruit, wholegrains, legumes, nuts & seeds) scores 1. ZOE publishes
# no fractions at all and gives coffee/tea/olive oil a FULL point — so the two
# popular systems disagree, and mixing them yields an incoherent total. We adopt
# Rossi's, which is the one that has fractions.
#
# We do NOT adopt Rossi's exclusions (juices, white/refined grains score 0). They
# are item-level, not species-level, and this counter is a union over species
# tokens — `white rice` and `米` are both Oryza sativa and must share the token
# `rice` or the union double-counts. An exclusion would have to re-split that
# token and reintroduce exactly the drift this module exists to prevent. The
# study counts refined grains anyway.
HERBS_SPICES = {
    "basil", "oregano", "thyme", "rosemary", "sage", "parsley", "cilantro",
    "mint", "dill", "bay leaf", "chive", "tarragon", "marjoram",
    "shiso", "mitsuba", "myoga", "sansho",
    "turmeric", "cumin", "coriander", "cinnamon", "clove", "nutmeg",
    "cardamom", "star anise", "fennel", "fenugreek", "saffron", "paprika",
    "vanilla", "mustard", "wasabi", "horseradish", "juniper",
    "black pepper", "chili pepper", "ginger",
}

# Rossi's other quarter-pointers: garlic, extra-virgin olive oil, tea, coffee.
QUARTER_OTHER = {"garlic", "olive", "tea", "coffee"}

WEIGHTS: dict[str, float] = {t: 0.25 for t in HERBS_SPICES | QUARTER_OTHER}
WEIGHTS["vegetable stock"] = 0.5
# refined to pure sucrose — nothing of the plant survives into the product
WEIGHTS["sugarcane"] = 0.0
WEIGHTS["sugar beet"] = 0.0

DEFAULT_WEIGHT = 1.0

# Tokens carrying no plant material at all — excluded under BOTH systems.
ZERO_WEIGHT = {"sugarcane", "sugar beet"}

# Which counting system the weekly total uses (user decision 2026-07-12):
#
#   "agp"   — American Gut Project (McDonald et al. 2018, mSystems), the study
#             that PRODUCED the number 30. A plain count of distinct plant
#             species: NO fractions, no exclusions. Its own survey instructions
#             say a can of soup with carrot+potato+onion counts as 3 plants, and
#             every grain in multigrain bread counts separately. Herbs, spices
#             and juices each score a full 1.0.
#   "rossi" — Megan Rossi's "plant points" (WEIGHTS above): herbs/spices/garlic/
#             olive oil/tea/coffee score ¼, vegetable stock ½. Nutritionally
#             sharper, but it keeps the TARGET at 30 while making 30 strictly
#             harder to reach — so the threshold no longer means what it meant in
#             the study it was taken from.
#
# We use AGP so that the TARGET and the COUNTING METHOD come from the same
# source. Flip this one constant to "rossi" to switch systems; nothing else
# changes (weight() is the single chokepoint).
COUNTING_MODE = "agp"

# Tokens that name no plant — a category, not a species. The LLM reaches for
# these when it does not know the answer ("jam" → "fruit"); they can neither be
# counted nor deduplicated, so they are dropped.
DROP = {
    "fruit", "fruits", "vegetable", "vegetables", "veggies", "plant", "plants",
    "grain", "grains", "cereal", "herb", "herbs", "spice", "spices",
    "nut", "nuts", "seed", "seeds", "bean", "beans", "legume", "legumes",
    "berry", "berries", "citrus fruit", "leafy greens", "greens", "produce",
    "flour", "oil", "starch", "gluten",
}

# ---------------------------------------------------------------------------
# Synonym → canonical. Context-free: these mean exactly one thing.
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    # --- Capsicum annuum, vegetable role -----------------------------------
    "capsicum": "bell pepper",
    "capsicum annuum": "bell pepper",
    "sweet pepper": "bell pepper",
    "sweet peppers": "bell pepper",
    "bell peppers": "bell pepper",
    "green bell pepper": "bell pepper",
    "red bell pepper": "bell pepper",
    "yellow bell pepper": "bell pepper",
    "orange bell pepper": "bell pepper",
    "green pepper": "bell pepper",
    "red pepper": "bell pepper",
    "pimento": "bell pepper",
    # --- Capsicum annuum, spice role ---------------------------------------
    "chilli pepper": "chili pepper",
    "chile pepper": "chili pepper",
    "chilli": "chili pepper",
    "chili": "chili pepper",
    "cayenne": "chili pepper",
    "cayenne pepper": "chili pepper",
    "jalapeno": "chili pepper",
    "hot pepper": "chili pepper",
    "red chili": "chili pepper",
    "togarashi": "chili pepper",
    # --- Piper nigrum, the actual "pepper" spice ----------------------------
    "peppercorn": "black pepper",
    "peppercorns": "black pepper",
    "white pepper": "black pepper",
    "piper nigrum": "black pepper",
    # --- citrus: the genus lump is never a valid token; the species are ------
    "mandarin orange": "mandarin",
    "tangerine": "mandarin",
    "satsuma": "mandarin",
    "mikan": "mandarin",
    "clementine": "mandarin",
    "sweet orange": "orange",
    "navel orange": "orange",
    "citrus sinensis": "orange",
    "yuzu citrus": "yuzu",
    # --- squashes (species-level; colour/cultivar does not split) -----------
    "zucchini": "summer squash",
    "courgette": "summer squash",
    "yellow squash": "summer squash",
    "crookneck squash": "summer squash",
    "cucurbita pepo": "summer squash",
    "butternut": "butternut squash",
    "kabocha squash": "kabocha",
    "japanese pumpkin": "kabocha",
    "winter squash": "kabocha",
    "acorn squash": "acorn squash",
    # --- alliums ------------------------------------------------------------
    "allium fistulosum": "scallion",
    "welsh onion": "scallion",
    "green onion": "scallion",
    "spring onion": "scallion",
    "negi": "scallion",
    "naganegi": "scallion",
    "white onion": "onion",
    "red onion": "onion",
    "yellow onion": "onion",
    "brown onion": "onion",
    "allium cepa": "onion",
    "shallot": "shallot",
    "garlic chives": "chinese chives",
    "nira": "chinese chives",
    # --- grains (colour/refinement does not split the species) --------------
    "oats": "oat",
    "rolled oats": "oat",
    "maize": "corn",
    "sweetcorn": "corn",
    "sweet corn": "corn",
    "white rice": "rice",
    "brown rice": "rice",
    "purple rice": "rice",
    "black rice": "rice",
    "wild rice": "rice",
    "oryza sativa": "rice",
    "whole wheat": "wheat",
    "wholewheat": "wheat",
    "wheat flour": "wheat",
    "durum wheat": "wheat",
    "semolina": "wheat",
    "spelt": "wheat",
    # --- mushrooms ----------------------------------------------------------
    "enoki mushroom": "enoki",
    "king oyster mushroom": "king oyster mushroom",
    "eringi": "king oyster mushroom",
    "shiitake mushroom": "shiitake",
    "shimeji mushroom": "shimeji",
    "maitake mushroom": "maitake",
    "white mushroom": "button mushroom",
    "cremini": "button mushroom",
    "portobello": "button mushroom",
    "agaricus bisporus": "button mushroom",
    # --- everything else ----------------------------------------------------
    "soy": "soybean",
    "soya": "soybean",
    "soybeans": "soybean",
    "edamame": "soybean",
    "aubergine": "eggplant",
    "coriander leaf": "cilantro",
    "chinese cabbage": "napa cabbage",
    "nappa cabbage": "napa cabbage",
    "hakusai": "napa cabbage",
    "pak choi": "bok choy",
    "pak choy": "bok choy",
    "chinese chard": "bok choy",
    "flax seed": "flax",
    "flaxseed": "flax",
    "linseed": "flax",
    "chia seed": "chia",
    "chia seeds": "chia",
    "sunflower seed": "sunflower",
    "pumpkin seeds": "pumpkin seed",
    "sesame seed": "sesame",
    "olives": "olive",
    "olive oil": "olive",
    "extra virgin olive oil": "olive",
    "green tea": "tea",
    "black tea": "tea",
    "camellia sinensis": "tea",
    "porphyra": "nori",
    "laver": "nori",
    "kombu": "kelp",
    "chrysanthemum": "shungiku",
    "garland chrysanthemum": "shungiku",
    "burdock root": "burdock",
    "lotus": "lotus root",
    "daikon radish": "daikon",
    "white radish": "daikon",
    "sour cherry": "tart cherry",
    "kiwi": "kiwifruit",
    "peanuts": "peanut",
    "walnuts": "walnut",
    "almonds": "almond",
    "green beans": "green bean",
    "string bean": "green bean",
    "black beans": "black bean",
    "mung beans": "mung bean",
    "bean sprout": "mung bean",
    "bean sprouts": "mung bean",
    "japanese plum": "plum",
    "ume": "plum",
    "sweet potatoes": "sweet potato",
    "tomatoes": "tomato",
    "cherry tomato": "tomato",
    "carrots": "carrot",
    "potatoes": "potato",
}

# ---------------------------------------------------------------------------
# Ambiguous bare tokens: the SAME token means different species depending on the
# item. Resolved against the item's own text (name + aliases), never guessed
# from the token alone.
#
#   "bell pepper bag"   → "pepper" → bell pepper   (vegetable)
#   "Amys frozen pizza" → "pepper" → black pepper  (seasoning — the default)
# ---------------------------------------------------------------------------
AMBIGUOUS: dict[str, tuple[list[tuple[str, str]], str | None]] = {
    "pepper": (
        [
            (r"bell|パプリカ|ピーマン|capsicum|sweet pepper|paprika", "bell pepper"),
            (r"chil[il]|唐辛子|cayenne|jalapen|hot pepper|togarashi|ラー油", "chili pepper"),
        ],
        # a bare "pepper" in anything else (a pizza, a sauce, a spice rack) is
        # the seasoning, Piper nigrum — not a capsicum
        "black pepper",
    ),
    "peppers": (
        [(r"chil[il]|唐辛子|hot", "chili pepper")],
        "bell pepper",
    ),
    "squash": (
        [
            (r"butternut", "butternut squash"),
            (r"kabocha|かぼちゃ|南瓜", "kabocha"),
            (r"acorn", "acorn squash"),
            (r"spaghetti squash", "spaghetti squash"),
        ],
        # yellow / crookneck / courgette / zucchini are all Cucurbita pepo
        "summer squash",
    ),
    "citrus": (
        [
            (r"lemon|レモン|檸檬", "lemon"),
            (r"lime|ライム", "lime"),
            (r"みかん|mikan|mandarin|tangerine|satsuma|clementine|温州", "mandarin"),
            (r"orange|オレンジ", "orange"),
            (r"grapefruit|グレープフルーツ", "grapefruit"),
            (r"yuzu|ゆず|柚子", "yuzu"),
            (r"すだち|sudachi", "sudachi"),
        ],
        # unattributable — a bare "citrus" would collide lemon with lime
        None,
    ),
    "pumpkin": (
        [
            (r"seed|シード|flour", "pumpkin seed"),
            (r"かぼちゃ|kabocha|南瓜", "kabocha"),
        ],
        "pumpkin",
    ),
    "mushroom": (
        [
            (r"shiitake|椎茸|しいたけ", "shiitake"),
            (r"shimeji|しめじ", "shimeji"),
            (r"maitake|舞茸|まいたけ", "maitake"),
            (r"enoki|えのき", "enoki"),
            (r"eringi|king oyster|エリンギ", "king oyster mushroom"),
        ],
        "button mushroom",
    ),
    "onion": (
        [(r"long onion|長ねぎ|小ねぎ|万能ねぎ", "scallion")],
        "onion",
    ),
    "cabbage": (
        [(r"白菜|hakusai|napa|chinese cabbage", "napa cabbage")],
        "cabbage",
    ),
    "cherry": (
        [(r"tart|sour|montmorency", "tart cherry")],
        "cherry",
    ),
    "rice": ([], "rice"),  # colour/refinement never splits Oryza sativa
}

_AMBIGUOUS_RX = {
    tok: ([(re.compile(rx, re.I), canon) for rx, canon in rules], default)
    for tok, (rules, default) in AMBIGUOUS.items()
}

_TOKEN = re.compile(r"^[a-z][a-z \-]{0,40}$")


def canonicalize(token: str, context: str = "") -> str | None:
    """One raw token → its canonical token, or None if it should be dropped.

    `context` is the item's own text (canonical name + display name + aliases),
    used only to resolve the ambiguous bare tokens above.
    """
    tok = " ".join(token.strip().lower().split())
    if not tok or not _TOKEN.match(tok):
        return None
    if tok in DROP:
        return None
    if tok in _AMBIGUOUS_RX:
        rules, default = _AMBIGUOUS_RX[tok]
        for rx, canon in rules:
            if rx.search(context):
                return canon
        return default
    # follow the alias map (one hop is enough; the map has no chains, but guard
    # anyway so a future edit cannot loop)
    seen = set()
    while tok in ALIASES and tok not in seen:
        seen.add(tok)
        tok = ALIASES[tok]
    return tok if tok not in DROP else None


def normalize(tokens, context: str = "") -> list[str]:
    """Raw LLM token list → canonical, de-duplicated, order-preserving."""
    out: list[str] = []
    for t in tokens or []:
        if not isinstance(t, str):
            continue
        canon = canonicalize(t, context)
        if canon and canon not in out:
            out.append(canon)
    return out


def weight(token: str) -> float:
    """Plant points for one canonical token, per COUNTING_MODE (the single
    chokepoint for the AGP-vs-Rossi choice)."""
    if token in ZERO_WEIGHT:
        return 0.0
    if COUNTING_MODE == "rossi":
        return WEIGHTS.get(token, DEFAULT_WEIGHT)
    return DEFAULT_WEIGHT  # AGP: flat count — every plant species scores 1


def score(tokens) -> float | int:
    """Plant points for a set of canonical tokens. Under AGP this is a plain
    integer count; under Rossi it is fractional (1 decimal)."""
    total = round(sum(weight(t) for t in set(tokens)), 1)
    return int(total) if total == int(total) else total


def countable(tokens) -> list[str]:
    """Canonical tokens that contribute > 0 points (drops sugarcane etc.)."""
    return [t for t in tokens if weight(t) > 0]


# The vocabulary the LLM is shown. Kept short on purpose: the alias map above is
# the safety net, this is only there to stop the obvious drift at the source.
VOCAB_RULES = (
    "PLANT TOKEN RULES (a wrong token silently double-counts the weekly total):\n"
    "- One token per SPECIES, lowercase English, singular. Colour, cultivar, "
    "brand and refinement NEVER change the token: green/red/yellow bell pepper "
    '-> "bell pepper"; white/red onion -> "onion"; white/brown/purple rice -> '
    '"rice"; whole wheat -> "wheat".\n'
    '- NEVER emit the bare token "pepper" — it is ambiguous. The sweet vegetable '
    '(ピーマン, パプリカ, capsicum) is "bell pepper"; the hot one is "chili pepper"; '
    'the black seasoning (Piper nigrum, in pizza/sauces/spice mixes) is "black pepper".\n'
    '- NEVER emit a genus or a category ("citrus", "squash", "fruit", "nuts", '
    '"grain", "mushroom"). Name the species: "lemon", "lime", "orange", '
    '"mandarin"; "summer squash" (= zucchini/yellow squash), "butternut squash", '
    '"kabocha"; "shiitake", "button mushroom".\n'
    "- Herbs and spices in a composite food ARE plants — list them "
    '(curry roux -> ["wheat","turmeric","cumin","coriander"]).\n'
    "- Non-plant foods (milk, meat, fish, egg, water) -> []."
)
