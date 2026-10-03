"""Relevance: is a store's product the item, or something made from it?
(PLAN.md Phase 8, delta 6, and the Codex review rounds that refined it.)

Every product name here is copied from, or shaped like, a cached Wegmans /
Whole Foods / ShopRite record — the rule exists because the cheapest-by-unit
ranking would otherwise pick rice cakes for rice, lemon juice for lemons and
egg noodles for eggs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import where



def test_a_percentage_names_the_product():
    """Codex review: '2% milk' and '1% milk' were the same words."""
    assert where.relevant("Wegmans 2% Reduced Fat Milk, 1 gal", "grass fed 2% milk") is False  # grass fed missing
    assert where.relevant("Grass Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Grass Fed 1% Milk, 64 fl oz", "grass fed 2% milk") is False


def test_relevance_checks_the_whole_leading_phrase_and_full_width_terms():
    """Codex review: 'Rice Cakes, Brown Rice' passed for brown rice, and a
    full-width '２％ milk' skipped every check."""
    assert where.relevant("Rice Cakes, Brown Rice, 16 oz", "brown rice") is False
    assert where.relevant("Lundberg Brown Rice, 2 lb", "brown rice") is True
    assert where.relevant("Honey Roasted Peanuts, 16 oz", "peanuts") is True
    assert where.relevant("1% Milk, 64 fl oz", "２％ milk") is False
    assert where.relevant("2% Milk, 64 fl oz", "２％ milk") is True


def test_excluded_product_types_are_folded_like_product_words():
    """Codex review: 'cookies' singular-folds to 'cooky' and slipped past."""
    assert where.relevant("Rice Cookies, 8 oz", "rice") is False
    assert where.relevant("Fruit Gummies, 6 oz", "fruit") is False


def test_the_item_must_lead_the_product_not_trail_it():
    """Codex review: cached 'organic lemon' results let lemon juices in when
    lemon only appeared after the first comma."""
    assert where.relevant("Simply Lemonade, made with real lemon", "lemon") is False
    assert where.relevant("Wegmans Organic Lemons, 2 lb", "organic lemon") is True
    # a store-brand lead segment is skipped, not mistaken for the product
    assert where.relevant("365 By Whole Foods Market, Tofu Firm Organic, 14 Ounce", "firm tofu",
                          "365 By Whole Foods Market") is True


def test_ingredients_after_with_do_not_change_the_product():
    """Codex review: a cached body lotion was rejected for 'with ... Coconut Oil'."""
    assert where.relevant("Shea Moisture Daily Hydration Body Lotion with Virgin Coconut Oil 16oz",
                          "body lotion") is True
    assert where.relevant("Wegmans Body Lotion Oil Blend", "body lotion") is False
    # and a match found only in the ingredient clause is not the item (Codex review)
    assert where.relevant("Body Lotion with Virgin Coconut Oil, 16 fl oz", "coconut oil") is False
    assert where.relevant("Nutiva Organic Coconut Oil, 15 fl oz", "coconut oil") is True


def test_cached_dill_relish_is_not_dill():
    """Codex review: the cached Whole Foods 'organic dill' results put relish first."""
    assert where.relevant("Organic Dill Relish, 10 oz", "organic dill") is False
    assert where.relevant("McCormick Gourmet Collection Organic Dill Weed, 0.5 oz", "organic dill") is True


def test_joined_and_separated_spellings_match():
    """Codex review: every cached Wegmans oat milk was rejected for 'oat milk'."""
    assert where.relevant("Wegmans Original Oatmilk, 64 fl oz", "oat milk") is True
    assert where.relevant("Oat Milk Barista, 32 fl oz", "oatmilk") is True
    assert where.relevant("Grassfed 2% Milk, 64 fl oz", "grass fed 2% milk") is True
    assert where.relevant("Oatmilk Creamer, 32 fl oz", "oat milk") is True       # creamer is not excluded
    assert where.relevant("Oat Cereal, 12 oz", "oat milk") is False


def test_brand_and_ingredient_words_do_not_name_the_item():
    """Codex review, cached products: 'Whole' only in the store brand, and
    'Pumpkin' only among a cat treat's ingredients."""
    assert where.relevant("365 By Whole Foods Market, Organic Baby Carrots, 2 lb", "whole carrot",
                          "365 By Whole Foods Market") is False
    assert where.relevant("Fancy Feast Savory Purees with Chicken & Pumpkin Cat Treats", "pumpkin puree") is False
    # a variety written after the comma still counts: Wegmans writes it that way
    assert where.relevant("Wegmans Organic Squash, Butternut", "butternut squash", "Wegmans") is True


def test_a_hyphenated_descriptor_is_not_an_ingredient_clause():
    """Codex review: 'Stir-In' split into 'stir' + 'in' cut the phrase before 'paste'."""
    assert where.relevant("Wegmans Organic Dill Stir-In Paste", "dill", "Wegmans") is False
    assert where.relevant("Grass-Fed 2% Milk, 64 fl oz", "grass fed 2% milk") is True


def test_a_size_in_the_item_name_is_not_a_product_word():
    """Found on cached data: 'salmon 2 lb' rejected every salmon fillet."""
    assert where.relevant("Sockeye Salmon Fillet, 32 oz", "organic salmon 2 lb") is True
    assert where.relevant("365 by Whole Foods Market Sockeye Salmon Fillets, 10 OZ", "salmon 2 lb",
                          "365 by Whole Foods Market") is True


def test_a_unit_word_is_a_size_only_after_a_number():
    """Codex review: 'eggs' was stripped as a count unit, leaving no words —
    so egg noodles passed for eggs."""
    assert where.relevant("Egg Noodles, 12 oz", "eggs") is False
    assert where.relevant("Large Brown Eggs, 12 ct", "eggs") is True
    assert where.relevant("Sockeye Salmon Fillet, 32 oz", "salmon 2 lb") is True


def test_brands_inline_and_brands_named_by_the_item():
    """Codex review, cached ShopRite: 'Pumpkin Tree' fruit puree is not
    pumpkin puree; 'Daisy, Sour Cream' is a Daisy sour cream."""
    assert where.relevant("Pumpkin Tree Strawberry & Banana Fruit Puree, 4 oz", "pumpkin puree",
                          "Pumpkin Tree") is False
    assert where.relevant("Farmer's Market Organic Pumpkin Puree, 15 oz", "pumpkin puree", "Farmer's Market") is True
    assert where.relevant("Daisy, Sour Cream, 16 Ounce", "Daisy sour cream", "Daisy") is True
    assert where.relevant("Breakstone's Sour Cream, 16 oz", "Daisy sour cream", "Breakstone's") is False


def test_whole_size_phrases_leave_the_item_name():
    """Codex review: 'milk 64 fl oz' kept 'fl' and 'oz' as required words."""
    assert where.relevant("Milk, 64 Fluid Ounces", "milk 64 fl oz") is True
    assert where.relevant("Paper Towels, 6 Rolls", "paper towels 600 sq ft") is True


def test_an_ingredient_list_further_on_does_not_name_the_item():
    """Codex review, cached: a baby puree listing pumpkin among its flavours."""
    assert where.relevant("Cerebelly Baby Puree, Organic, White Bean, Pumpkin, Apple with Cinnamon",
                          "pumpkin puree", "Cerebelly") is False
    assert where.relevant("Wegmans Organic Squash, Butternut", "butternut squash", "Wegmans") is True


def test_descriptors_in_later_segments_still_count_and_frosting_is_not_cheese():
    """Codex review, cached: SoyBoy and Organic Valley put descriptors in the
    third segment; Duncan Hines frosting is not whipped cream cheese."""
    assert where.relevant("SoyBoy Tofu, Organic, Extra Firm", "extra firm tofu", "SoyBoy") is True
    assert where.relevant("Organic Valley Cheese Slices, Non-Smoked, Provolone", "cheese slices provolone",
                          "Organic Valley") is True
    assert where.relevant("Duncan Hines Whipped Cream Cheese Frosting", "whipped cream cheese", "Duncan Hines") is False


def test_brand_only_items_and_ie_plurals():
    """Codex review: 'Nutella' and 'Cheetos' matched nothing; 'cookie' never
    matched 'Cookies'."""
    assert where.relevant("Nutella Hazelnut Spread, 13 oz", "Nutella", "Nutella") is True
    assert where.relevant("Jif Creamy Peanut Butter", "Nutella", "Jif") is False
    assert where.relevant("Chocolate Chip Cookies, 12 oz", "cookie") is True
    assert where.relevant("Chocolate Brownies, 12 oz", "brownie") is True
    assert where.relevant("Oatmeal Cookie, 2 oz", "cookies") is True


def test_a_variety_in_a_later_segment_is_where_the_item_is_named():
    """Codex review: 'provolone' was only looked for in the first segment."""
    assert where.relevant("Organic Valley Cheese Slices, Non-Smoked, Provolone", "provolone", "Organic Valley") is True


def test_a_count_that_is_the_items_noun_is_kept():
    """Codex review: '12 eggs' stripped to nothing, so egg noodles passed."""
    assert where.relevant("Egg Noodles, 12 oz", "12 eggs") is False
    assert where.relevant("Large Brown Eggs, 12 ct", "12 eggs") is True
