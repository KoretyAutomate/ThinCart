"""Relevance: does a store's product name the item? (PLAN.md §2026-10-04)

A plain word check: every content word of the item appears in the product's
own words — brand and sizes removed, joined or apart, plural-folded. WHICH
matching product is the item is decided by the store's own ranking (its first
fitting result) — see tests/test_where.py. The names below are copied from, or
shaped like, cached Wegmans / Whole Foods / ShopRite records.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import where


def test_every_item_word_must_appear():
    assert where.relevant("Wegmans Long Grain White Rice, 2 lb Bag", "white rice") is True
    assert where.relevant("Wegmans Paper Towels, 12 Rolls", "printing paper") is False
    assert where.relevant("Del Monte Fresh Cut Leaf Spinach", "basil leaves fresh") is False


def test_a_percentage_names_the_product():
    assert where.relevant("Grass Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Grass Fed 1% Milk, 64 fl oz", "grass fed 2% milk") is False
    assert where.relevant("1% Milk, 64 fl oz", "２％ milk") is False         # full-width, folded first


def test_joined_and_separated_spellings_and_word_forms():
    assert where.relevant("Wegmans Original Oatmilk, 64 fl oz", "oat milk") is True
    assert where.relevant("Oat Milk Barista, 32 fl oz", "oatmilk") is True
    assert where.relevant("Grass-Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Birds Eye Riced Cauliflower, 10 oz", "frozen cauliflower rice") is True
    assert where.relevant("Chocolate Chip Cookies, 12 oz", "cookie") is True
    assert where.relevant("Oatmeal Cookie, 2 oz", "cookies") is True


def test_cuts_and_descriptors_count_wherever_the_store_writes_them():
    """Codex round 23: meat cuts, and descriptors in later segments."""
    assert where.relevant("Chicken Breast, Boneless Skinless, 1 lb", "chicken") is True
    assert where.relevant("SoyBoy Tofu, Organic, Extra Firm, 14 oz", "extra firm tofu", "SoyBoy") is True
    assert where.relevant("Organic Valley Cheese Slices, Non-Smoked, Provolone", "provolone", "Organic Valley") is True
    assert where.relevant("Wegmans Organic Squash, Butternut", "butternut squash", "Wegmans") is True


def test_the_brand_does_not_name_the_item_unless_the_item_names_the_brand():
    assert where.relevant("365 By Whole Foods Market, Organic Baby Carrots, 2 lb", "whole carrot",
                          "365 By Whole Foods Market") is False
    assert where.relevant("Pumpkin Tree Strawberry & Banana Fruit Puree, 4 oz", "pumpkin puree",
                          "Pumpkin Tree") is False
    assert where.relevant("Daisy, Sour Cream, 16 Ounce", "Daisy sour cream", "Daisy") is True
    assert where.relevant("Breakstone's Sour Cream, 16 oz", "Daisy sour cream", "Breakstone's") is False
    assert where.relevant("Nutella Hazelnut Spread, 13 oz", "Nutella", "Nutella") is True
    assert where.relevant("Jif Creamy Peanut Butter", "Nutella", "Jif") is False


def test_sizes_in_the_item_name_are_not_required_words():
    assert where.relevant("Sockeye Salmon Fillet, 32 oz", "organic salmon 2 lb") is True
    assert where.relevant("Milk, 64 Fluid Ounces", "milk 64 fl oz") is True
    assert where.relevant("Paper Towels, 6 Rolls", "paper towels 600 sq ft") is True
    # ...but a count that is the item's own noun stays: "12 eggs" is eggs
    assert where.relevant("Large Brown Eggs, 12 ct", "12 eggs") is True
    assert where.relevant("Egg Noodles, 12 oz", "12 eggs") is True    # names eggs; the store's ranking decides


def test_an_item_with_no_english_name_cannot_be_checked():
    assert where.relevant("Anything at all", "キッチンペーパー") is True


def test_brands_with_ignored_words_and_counted_nouns():
    """Codex review: 'Fresh Express' and 'Organic Valley' items, and eggs
    counted in the product name."""
    assert where.relevant("Fresh Express Spinach, 8 oz", "Fresh Express spinach", "Fresh Express") is True
    assert where.relevant("Organic Valley Whole Milk, 64 fl oz", "Organic Valley whole milk", "Organic Valley") is True
    assert where.relevant("Organic Free Range 12 Eggs", "eggs") is True
