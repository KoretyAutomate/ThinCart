"""quantity.py — package sizes from real store text (PLAN.md Phase 8.2/8.3).

The strings are copied from cached Wegmans / Whole Foods / ShopRite records:
the parser exists because those stores cannot be compared on their own unit
prices (sq ft vs sheets vs each), and Whole Foods never fills a pack size.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import quantity as q


@pytest.mark.parametrize("text, want", [
    ("16 ounce", {"weight": 16.0}),
    ("2 fl. oz.", {"volume": 2.0}),
    ("2 lb.", {"weight": 32.0}),
    ("627.3 sq. ft.", {"area": 627.3}),
    ("1 gallon", {"volume": 128.0}),
    ("12 ct.", {"each": 12.0}),
    ("Oatmilk Unsweetened, 48 Fl Oz", {"volume": 48.0}),
    ("Fusilli #34, 16 Ounce", {"weight": 16.0}),
    ("6 x 16 oz bottles", {"weight": 96.0}),
    ("Seventh Generation 100% Recycled Paper Towels, 2 Ply, 140 Sheets", {"sheet": 140.0}),
    # sheets per roll x rolls, with size adjectives between number and unit
    ("Bounty Paper Towels Select-A-Size White, 6 Double Plus Rolls, 103 Sheets Per Roll",
     {"roll": 6.0, "sheet": 618.0}),
    # a nutrition fact is not the package
    ("Hodo Extra Firm Organic Tofu | 48g Protein (96% Daily Value) per Pkg", {}),
    ("Sweet Onion", {}),
    # Codex plan review: a dimension stated twice with different amounts is ambiguous
    ("365 by Whole Foods Market Recycled Paper Towel 6ct, 110 CT", {}),
    ("Seventh Generation Paper Towels, 140 Sheets, 6 Rolls", {"sheet": 140.0, "roll": 6.0}),
])
def test_parse(text, want):
    assert q.parse(text) == want


def test_the_stores_unit_price_implies_only_physical_sizes():
    assert q.from_unit_price(6.79, "$0.68/ounce") == {"weight": 9.985}
    assert q.from_unit_price(3.49, "$3.49/lb") == {"weight": 16.0}
    # counts, sheets and rolls are never derived from a store's unit price
    assert q.from_unit_price(6.29, "$6.29/count") == {}
    assert q.from_unit_price(7.79, "$2.78/Sheets") == {}
    assert q.from_unit_price(None, "$1/oz") == {}


def test_quantities_from_real_records():
    # pack size wins; a rounded unit price ($0.03/sq ft) is not a contradiction
    rec = {"pack_size": "627.3 sq. ft.", "amount": 15.99, "unit_price": "$0.03/sq. ft.",
           "name": "Wegmans Paper Towels, Choose-A-Size, 12 Rolls, Family Pack"}
    assert q.quantities(rec) == {"area": 627.3, "roll": 12.0}
    # Whole Foods: no pack size, size from the name, checked by the unit price
    rec = {"pack_size": "", "amount": 7.49, "unit_price": "$0.47/ounce",
           "name": "Vital Farms Pasture-Raised Liquid Whole Eggs 16 oz"}
    assert q.quantities(rec)["weight"] == 16.0
    # no size in the name: the unit price fills it
    rec = {"pack_size": "", "amount": 6.79, "unit_price": "$0.68/ounce", "name": "Hodo Extra Firm Organic Tofu"}
    assert q.quantities(rec) == {"weight": 9.985}
    # a name size the unit price contradicts (here: 4x off) is dropped, not trusted
    rec = {"pack_size": "", "amount": 8.0, "unit_price": "$0.50/ounce", "name": "Thing, 4 oz"}
    assert "weight" not in q.quantities(rec)


def test_oz_and_fl_oz_compare_only_for_liquids():
    assert q.comparable({"weight": 59.0}, liquid=True) == {"weight": 59.0, "volume": 59.0}
    assert q.comparable({"volume": 64.0, "weight": 70.0}, liquid=True) == {"volume": 64.0, "weight": 70.0}
    # 12 oz of honey is not 12 fl oz (Codex review)
    assert q.comparable({"weight": 12.0}) == {"weight": 12.0}
    assert q.is_liquid("oat milk") and q.is_liquid("Tart Cherry Juice") and not q.is_liquid("honey")
    # both dry and liquid products exist: weight and volume stay apart (Codex review)
    assert not q.is_liquid("coffee") and not q.is_liquid("green tea") and not q.is_liquid("cream")


@pytest.mark.parametrize("text, want", [
    ("2 lb", ("weight", 32.0)), ("6 rolls", ("roll", 6.0)), ("1 gal", ("volume", 128.0)),
    ("12", ("each", 12.0)), ("two pounds", None), ("", None), ("2 lb 6 rolls", None),
    # Codex review: never the tail of a number
    (".5 lb", ("weight", 8.0)), ("1/2 lb", ("weight", 8.0)), ("1 1/2 lb", ("weight", 24.0)),
    # Codex review round 2: unusual numbers are read whole or not at all
    ("1/0 lb", None), ("½ lb", ("weight", 8.0)), ("1,100 sheets", ("sheet", 1100.0)),
    ("1½ lb", ("weight", 24.0)), ("1-1/2 lb", ("weight", 24.0)),
])

def test_parse_wanted(text, want):
    assert q.parse_wanted(text) == want


def test_cost_to_cover_buys_enough_and_no_more():
    assert q.cost_to_cover(3.0, 15.9, 16) == (3.0, 1)      # within 5%: one jar covers a pound
    assert q.cost_to_cover(3.0, 15.0, 16) == (6.0, 2)
    # the huge bag is cheaper per oz but costs more to cover what is wanted
    big, small = q.cost_to_cover(20.0, 320, 32), q.cost_to_cover(5.0, 32, 32)
    assert small < big


def test_choose_dim():
    assert q.choose_dim([{"weight": 1}, {"weight": 2, "each": 1}, {"each": 3}]) == "weight"
    assert q.choose_dim([{"sheet": 1, "roll": 2}, {"area": 3}, {"sheet": 4}]) == "sheet"
    assert q.choose_dim([{}, {}]) is None
    assert q.choose_dim([{"weight": 1}], wanted="each") == "each"


def test_labels():
    assert q.unit_label(9.89, 660, "sheet") == "$1.50/100 sheets"
    assert q.unit_label(15.99, 627.3, "area") == "$0.03/sq ft"
    assert q.qty_label(32, "weight") == "2 lb" and q.qty_label(12, "weight") == "12 oz"
    assert q.qty_label(6, "roll") == "6 rolls"
