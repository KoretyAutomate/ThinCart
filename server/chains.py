"""
chains.py — which shops have a price source, and the shape every source shares.

Pure: no network, no database. lookup.py owns the outbound POLICY (kill switch,
cache, rate limit) and is the only module that reaches outward; wegmans.py,
wholefoods.py and shoprite.py each know what ONE chain's answers mean. This
module is the small amount of knowledge that sits above all three:

  - how a store the household typed or pinned is recognised as a branch of a
    chain we can ask (`detect`),
  - how a shelf position is written on screen, whichever chain it came from
    (`aisle_label`),
  - how a town and state are read off an OpenStreetMap address, which every
    chain's branch lookup starts from.

The record shape is the contract between the adapters and everything above
them. Every adapter returns dicts with these keys, and lookup_api.py never asks
which chain a record came from — it reads `source` for display and nothing else.

    sku, name, brand, sub_brand, pack_size, upc, store_number,
    amount (float | None), unit_price (str), available (bool),
    aisle, aisle_side, section, shelf (all str, "" when unknown),
    source (the chain slug), source_url (a page a human can open),
    fetched_at (ISO time the fact was read)

`aisle` is either a number ("14B", "9") or a department ("Dairy"). Wegmans
carries side and section, ShopRite carries shelf, Whole Foods carries the
department only. An empty string is "unknown" and must be rendered as such —
never as a plausible blank.
"""

import re
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt


@dataclass(frozen=True)
class Chain:
    slug: str
    label: str
    # Lower-cased substrings of the store name or OSM brand that identify it.
    needles: tuple[str, ...]


CHAINS: dict[str, Chain] = {
    "wegmans": Chain("wegmans", "Wegmans", ("wegmans",)),
    "wholefoods": Chain("wholefoods", "Whole Foods", ("whole foods", "wholefoods")),
    "shoprite": Chain("shoprite", "ShopRite", ("shoprite", "shop rite", "shop-rite")),
}


def detect(name: str, brand: str = "") -> str:
    """The chain slug for a store, or "" when it is not one we can ask.

    Most stores are not. That is ordinary, and every caller has to read
    correctly in that case: the answer is "no price source", not an error.
    """
    haystack = f"{name} {brand}".lower()
    for chain in CHAINS.values():
        if any(n in haystack for n in chain.needles):
            return chain.slug
    return ""


# Every state, because this parser now gates a nationwide chain. It began as
# the ten Wegmans trades in, and the gate caught that a Whole Foods in Austin
# would have been "could not read a town from the address".
US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "district of columbia": "dc",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}


def address_town_state(address: str) -> tuple[str, str]:
    """('princeton', 'nj') from an OpenStreetMap display address such as
    '240 Nassau Park Blvd, Princeton, Mercer County, New Jersey, 08540, USA'.

    ('', '') when it does not parse. Never a guess: a guessed town resolves to
    some other branch, and everything is then mis-priced with no visible sign.
    The town comes back slug-shaped (lower case, hyphens) because that is what
    every chain's branch page is keyed by.
    """
    parts = [p.strip() for p in address.split(",")]
    state = idx = None
    for i, p in enumerate(parts):
        if p.lower() in US_STATES:
            state, idx = US_STATES[p.lower()], i
            break
    if state is None or idx is None:
        return "", ""
    for j in range(idx - 1, -1, -1):
        if parts[j].lower().endswith("county"):
            continue
        town = re.sub(r"[^a-z0-9]+", "-", parts[j].lower()).strip("-")
        if town:
            return town, state
    return "", ""


def town_from_name(name: str, chain: str) -> str:
    """'Whole Foods Montgomery' -> 'montgomery'; 'ShopRite of Ewing, NJ' -> 'ewing'.

    For a store OpenStreetMap has never heard of — a branch that opened last
    month — the name the household typed is the only clue to which branch it
    is, and the chain's own directory is what can turn a town into a branch.
    Empty when nothing but the chain's name is left.
    """
    s = name.lower()
    for needle in sorted(CHAINS[chain].needles, key=len, reverse=True) if chain in CHAINS else ():
        s = s.replace(needle, " ")
    s = re.sub(r"\b(market|store|supermarket|of|the|at|in)\b", " ", s)
    # "Princeton, New Jersey" and "Princeton NJ" both mean Princeton.
    states = "|".join(sorted(list(US_STATES) + list(US_STATES.values()), key=len, reverse=True))
    s = re.sub(r"[\s,]+(" + states + r")\.?\s*$", " ", s.strip())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def close(lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None,
          km: float = 1.5) -> bool:
    """Whether two points are the same shop, give or take a car park. False when
    either is unknown — an unknown position confirms nothing."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return False
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a)) <= km


def postcode(address: str) -> str:
    """The five-digit ZIP in an address, or ''. The most exact branch key there
    is: two ShopRites can share a town, and none share a ZIP."""
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
    return m.group(1) if m else ""


def aisle_label(rec: dict) -> str:
    """"Aisle 14B · left · sec 11", "Aisle 9 · shelf 7", "Dairy" — a findable
    instruction rather than a bare number. Empty when there is no aisle, which
    callers must render as unknown rather than as a plausible blank.
    """
    head = (rec.get("aisle") or "").strip()
    if not head:
        return ""
    # The field is not always a number: perishables come back as a department
    # ("Dairy", "Produce"). "Aisle Dairy" reads as a mistake, so the word is
    # only added where it is actually an aisle.
    parts = [f"Aisle {head}" if head[:1].isdigit() else head]
    side = {"L": "left", "R": "right"}.get((rec.get("aisle_side") or "").upper())
    if side:
        parts.append(side)
    if rec.get("section"):
        parts.append(f"sec {rec['section']}")
    elif rec.get("shelf") and head[:1].isdigit():
        # ShopRite places centre-store goods by aisle and shelf. Wegmans
        # carries a shelf too but has always been labelled without it: section
        # already pins the spot on a numbered aisle, and a department ("Dairy")
        # is the whole instruction — "Dairy · shelf 2" says nothing findable.
        parts.append(f"shelf {rec['shelf']}")
    return " · ".join(parts)
