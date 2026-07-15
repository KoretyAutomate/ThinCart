"""Canonical plant vocabulary + weighted plant-point counting.

Regressions from live data (2026-07-12): `bell pepper bag` → ["pepper"] but
`green bell pepper` → ["capsicum"] (same species, two tokens → double-count);
`Amys frozen pizza` → ["pepper"] meaning the SPICE, colliding with the vegetable
under one token; `レモン`+`Lime` → ["citrus"] (genus lump), `オレンジジュース` →
["orange"] (species) — inconsistent granularity.
"""
import contextlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import plants  # noqa: E402


# --------------------------------------------------------------------------
# normalization: synonyms fold onto one canonical token
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,canon", [
    ("capsicum", "bell pepper"),
    ("green bell pepper", "bell pepper"),
    ("sweet pepper", "bell pepper"),
    ("Capsicum", "bell pepper"),          # case-insensitive
    ("peppercorn", "black pepper"),
    ("cayenne", "chili pepper"),
    ("white onion", "onion"),             # colour never splits a species
    ("red onion", "onion"),
    ("white rice", "rice"),               # nor does refinement
    ("brown rice", "rice"),
    ("purple rice", "rice"),
    ("zucchini", "summer squash"),
    ("yellow squash", "summer squash"),   # both Cucurbita pepo
    ("butternut", "butternut squash"),
    ("oats", "oat"),
    ("maize", "corn"),
    ("allium fistulosum", "scallion"),
    ("mandarin orange", "mandarin"),
    ("edamame", "soybean"),
    ("olive oil", "olive"),
])
def test_alias_map_folds_synonyms(raw, canon):
    assert plants.normalize([raw]) == [canon]


def test_generic_category_tokens_are_dropped():
    """'jam' → ["fruit"] names no species: it can neither be counted nor deduped."""
    assert plants.normalize(["fruit", "nuts", "grain", "vegetables"]) == []


def test_normalize_dedupes_after_folding():
    """The whole point: two spellings of one species collapse to ONE token."""
    assert plants.normalize(["capsicum", "bell pepper", "pepper"],
                            "green bell pepper") == ["bell pepper"]


def test_normalize_drops_non_ascii_and_junk():
    assert plants.normalize(["小麦", "wheat", 7, "", "Wheat"]) == ["wheat"]


# --------------------------------------------------------------------------
# the collision: one token, two unrelated species — resolved by item context
# --------------------------------------------------------------------------
def test_pepper_collision_resolved_by_item_context():
    veg = plants.normalize(["pepper"], "bell pepper bag")
    spice = plants.normalize(["wheat", "tomato", "basil", "pepper"], "amys frozen pizza")
    assert veg == ["bell pepper"]                       # Capsicum annuum, 1 point
    assert spice[-1] == "black pepper"                  # Piper nigrum, ¼ point
    # and they no longer collide: the union of a pizza week and a pepper week
    # contains BOTH plants, not one
    assert set(veg) & set(spice) == set()


def test_bare_pepper_defaults_to_the_spice():
    """In an item with no capsicum cue, 'pepper' is the seasoning."""
    assert plants.normalize(["pepper"], "tomato pasta sauce") == ["black pepper"]


def test_citrus_lump_is_split_by_species():
    """レモン and Lime both said 'citrus' → they used to be ONE plant. They are two."""
    lemon = plants.normalize(["citrus"], "レモン lemon")
    lime = plants.normalize(["citrus"], "lime")
    mikan = plants.normalize(["citrus"], "みかん")
    assert lemon == ["lemon"] and lime == ["lime"] and mikan == ["mandarin"]
    assert len({lemon[0], lime[0], mikan[0]}) == 3
    # unattributable citrus is dropped rather than colliding lemon with lime
    assert plants.normalize(["citrus"], "mystery drink") == []


def test_squash_granularity_is_consistent():
    assert plants.normalize(["squash"], "yellow squash") == ["summer squash"]
    assert plants.normalize(["butternut squash"], "butternut squash") == ["butternut squash"]
    assert plants.normalize(["squash"], "Butternut squash") == ["butternut squash"]
    assert plants.normalize(["pumpkin"], "かぼちゃ") == ["kabocha"]
    # a pumpkin SEED is a seed (full point), not the vegetable
    assert plants.normalize(["pumpkin"], "simple mills organic seed flour") == ["pumpkin seed"]


# --------------------------------------------------------------------------
# weights
# --------------------------------------------------------------------------
@contextlib.contextmanager
def counting_mode(mode):
    """Flip plants.COUNTING_MODE for the duration of a test."""
    orig = plants.COUNTING_MODE
    plants.COUNTING_MODE = mode
    try:
        yield
    finally:
        plants.COUNTING_MODE = orig


def test_agp_weights_are_a_flat_count():
    """Default mode: the study's own method — every plant species scores 1.0,
    no fractions (a can of soup with carrot+potato+onion counts as 3)."""
    assert plants.COUNTING_MODE == "agp"
    for token in ("kale", "walnut", "rice", "basil", "black pepper",
                  "garlic", "olive", "tea", "coffee", "tart cherry"):
        assert plants.weight(token) == 1.0, token
    # carries no plant material at all — excluded under BOTH systems
    assert plants.weight("sugarcane") == 0.0


def test_rossi_weights_follow_the_plant_point_rules():
    """The fractional system stays available behind COUNTING_MODE="rossi"."""
    with counting_mode("rossi"):
        assert plants.weight("kale") == 1.0            # vegetable
        assert plants.weight("walnut") == 1.0          # nuts & seeds are a full point
        assert plants.weight("rice") == 1.0            # we do not exclude refined grains
        assert plants.weight("basil") == 0.25          # herb
        assert plants.weight("black pepper") == 0.25   # spice
        assert plants.weight("garlic") == 0.25
        assert plants.weight("olive") == 0.25          # extra-virgin olive oil
        assert plants.weight("tea") == 0.25
        assert plants.weight("coffee") == 0.25
        assert plants.weight("sugarcane") == 0.0


def test_score_agp_is_a_headcount_rossi_is_weighted():
    tokens = ["tomato", "kale", "basil", "oregano", "garlic", "black pepper"]
    # AGP: 6 distinct species → 6 points, herbs/spices included at full value
    assert plants.score(tokens) == 6
    # Rossi: 2 vegetables + 4 quarter-pointers = 2 + 1.0 = 3.0
    with counting_mode("rossi"):
        assert plants.score(tokens) == 3.0
    assert plants.score([]) == 0
    # duplicates cannot inflate the score under either system
    assert plants.score(["kale", "kale"]) == 1


def test_countable_drops_zero_weight_tokens():
    assert plants.countable(["wheat", "sugarcane"]) == ["wheat"]


# --------------------------------------------------------------------------
# end-to-end through the DB counter
# --------------------------------------------------------------------------
def seed(name, days_ago, plants_json):
    cid = db.get_or_create_catalog(appmod.conn, name)
    appmod.conn.execute(
        "UPDATE item_catalog SET plants_json=?, is_edible=1, llm_enriched_at='x' WHERE id=?",
        (json.dumps(plants_json, ensure_ascii=False), cid),
    )
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    appmod.conn.execute(
        "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)", (cid, ts)
    )
    appmod.conn.commit()
    return cid


def test_weekly_count_is_deduped_across_drifted_rows():
    """The live bug, end to end: two rows, two tokens, one species → ONE point."""
    seed("bell pepper bag", 1, ["pepper"])          # drifted token, vegetable
    seed("green bell pepper", 2, ["capsicum"])      # drifted token, same species
    seed("herby thing", 1, ["basil", "oregano"])

    week = catalog.weekly_plants(appmod.conn)
    assert week.count("bell pepper") == 1
    assert "capsicum" not in week and "pepper" not in week

    # stale rows are re-canonicalized on READ — no re-enrichment needed, so the
    # count is right even with the LLM down
    assert catalog.weekly_score(appmod.conn) == plants.score(week)
    # the drifted pair collapses to ONE point, not two (normalize is what dedupes;
    # score() takes tokens that are already canonical)
    assert plants.score(plants.normalize(["pepper", "capsicum"],
                                         "green bell pepper")) == 1


def test_state_reports_count_and_weights():
    seed("garlic bulb", 1, ["garlic"])
    p = db.state(appmod.conn)["plants"]
    assert p["target"] == 30
    assert p["count"] == plants.score(p["week"])
    # AGP is a flat count → integral total and NO fractional weights to report
    assert p["count"] == int(p["count"])
    assert p["weights"] == {}
    with counting_mode("rossi"):
        p = db.state(appmod.conn)["plants"]
        assert p["weights"]["garlic"] == 0.25
        # full-point plants are absent from `weights` (only exceptions travel).
        # NB: the suite shares one DB across files — do not seed a plant another
        # test asserts the absence of (test_plants.py owns `kale`).
        seed("collard bag", 1, ["collard"])
        assert "collard" not in db.state(appmod.conn)["plants"]["weights"]


def test_counter_works_with_llm_down():
    """PLAN.md constraint: the counter is rule-based and never calls the LLM."""
    import llm

    def _boom(*a, **kw):
        raise AssertionError("counter must not touch the LLM")

    orig = llm.chat_json
    llm.chat_json = _boom
    try:
        seed("miso", 2, ["soybean"])
        assert "soybean" in catalog.weekly_plants(appmod.conn)
        assert catalog.weekly_score(appmod.conn) > 0
    finally:
        llm.chat_json = orig
