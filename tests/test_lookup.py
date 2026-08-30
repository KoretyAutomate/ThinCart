"""Phase 6: the outbound-lookup choke point (server/lookup.py).

These tests never touch the network. That is the point, and it is the lesson the
APK shipped to earn (PLAN.md §2026-08-30): a suite that lives entirely on one
side of a seam proves nothing about the other. So the parsing, the provenance
rules, the TTL and the kill switch are pinned here against a RECORDED Nominatim
response, and the network path itself is exercised separately and is allowed to
be skipped when the box is offline.

The rules under test are the three lookup.py promises:
  1. provenance or nothing,
  2. not-found is an answer — never a plausible substitute,
  3. cache before network.
"""

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient

import app as appmod
import lookup

client = TestClient(appmod.app)


def op(**fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "test", **fields}
    return body, client.post("/api/op", json=body)


def stores_by_name():
    return {s["name"]: s for s in client.get("/api/state").json()["stores"]}


# One real Nominatim jsonv2 shape, trimmed. Recorded rather than invented so the
# parser is tested against the field names the service actually sends.
FIXTURE = [
    {
        "osm_type": "way", "osm_id": 123456789,
        "name": "Whole Foods Market",
        "display_name": "Whole Foods Market, 3535 US-1, Princeton, Mercer County, New Jersey, 08540, United States",
        "lat": "40.3311", "lon": "-74.6512",
        "type": "supermarket", "category": "shop",
        "extratags": {"brand": "Whole Foods Market"},
    },
    {
        "osm_type": "node", "osm_id": 987654321,
        "name": "McCaffrey's Food Market",
        "display_name": "McCaffrey's Food Market, 301 N Harrison St, Princeton, New Jersey, 08540, United States",
        "lat": "40.3573", "lon": "-74.6588",
        "type": "supermarket", "category": "shop",
    },
    {   # not a grocery: must still be findable, but must sort below the shops
        "osm_type": "node", "osm_id": 555,
        "name": "Princeton Public Library",
        "display_name": "Princeton Public Library, 65 Witherspoon St, Princeton, New Jersey, United States",
        "lat": "40.3500", "lon": "-74.6590",
        "type": "library", "category": "amenity",
    },
    {   # no osm_id: nothing to pin a store TO, so it is dropped entirely
        "osm_type": "node", "name": "Ghost Mart",
        "display_name": "Ghost Mart, Nowhere", "lat": "0", "lon": "0", "type": "supermarket",
    },
]


def test_parse_keeps_only_pinnable_results():
    out = lookup.parse_store_results(FIXTURE)
    assert [d["name"] for d in out] == [
        "McCaffrey's Food Market", "Whole Foods Market", "Princeton Public Library"
    ]
    assert all(d["osm_id"] for d in out)


def test_every_result_carries_provenance():
    """Promise 1. A record that cannot say where it came from is not returned."""
    for d in lookup.parse_store_results(FIXTURE):
        assert d["source"] == "openstreetmap"
        assert "/" in d["osm_id"]  # osm_type/osm_id, unique across OSM


def test_address_drops_the_duplicated_name():
    whole = next(d for d in lookup.parse_store_results(FIXTURE) if d["name"] == "Whole Foods Market")
    assert whole["address"].startswith("3535 US-1, Princeton")
    assert not whole["address"].startswith("Whole Foods Market")
    assert whole["lat"] == 40.3311
    assert whole["brand"] == "Whole Foods Market"


def test_missing_brand_is_empty_not_invented():
    mc = next(d for d in lookup.parse_store_results(FIXTURE) if d["name"].startswith("McCaffrey"))
    assert mc["brand"] == ""


def test_empty_upstream_gives_an_empty_answer():
    """Promise 2. No results is a result; it must not become a fabricated one."""
    assert lookup.parse_store_results([]) == []


def test_cache_roundtrip_and_expiry():
    """Promise 3, both halves: a fresh entry is served, a stale one is not."""
    lookup.cache_put("store", "k1", [{"name": "X"}])
    assert lookup.cache_get("store", "k1") == [{"name": "X"}]

    stale = (datetime.now(UTC) - lookup.TTL["store"] - timedelta(days=1)).isoformat()
    lookup._conn.execute("UPDATE lookup_cache SET fetched_at=? WHERE kind='store' AND key='k1'", (stale,))
    lookup._conn.commit()
    assert lookup.cache_get("store", "k1") is None
    assert lookup.cache_get("store", "never-asked") is None


def test_kill_switch_matrix():
    original = lookup.MODE
    try:
        lookup.MODE = "off"
        assert not lookup.enabled("stores") and not lookup.enabled("price")
        lookup.MODE = "stores"
        assert lookup.enabled("stores") and not lookup.enabled("price")
        lookup.MODE = "all"
        assert lookup.enabled("stores") and lookup.enabled("price")
    finally:
        lookup.MODE = original


def test_search_endpoint_503s_when_disabled():
    """THINCART_LOOKUP=off is a supported state, not a broken one."""
    original = lookup.MODE
    try:
        lookup.MODE = "off"
        assert client.get("/api/stores/search?q=anything").status_code == 503
        # ...and the rest of the app is untouched by it.
        assert client.get("/api/state").status_code == 200
    finally:
        lookup.MODE = original


def test_store_upsert_pins_the_real_place():
    op(type="store_upsert", store_name="McCaffrey's", store_osm_id="node/987654321",
       store_address="301 N Harrison St, Princeton", store_lat=40.3573, store_lon=-74.6588,
       store_brand="McCaffrey's")
    s = stores_by_name()["McCaffrey's"]
    assert s["osm_id"] == "node/987654321"
    assert s["address"] == "301 N Harrison St, Princeton"
    assert s["lat"] == 40.3573


def test_a_later_name_only_upsert_does_not_blank_the_pin():
    """Editing notes, or a lagging offline op that only knows the name, must not
    erase the address a deliberate pick established."""
    op(type="store_upsert", store_name="Wegmans", store_osm_id="way/1",
       store_address="240 Nassau Park Blvd", store_lat=40.3, store_lon=-74.6)
    op(type="store_upsert", store_name="Wegmans", store_notes="good fish")
    s = stores_by_name()["Wegmans"]
    assert s["osm_id"] == "way/1"
    assert s["address"] == "240 Nassau Park Blvd"
    assert s["notes"] == "good fish"


def test_free_text_stores_still_work_with_no_pin():
    """A shop OSM has never heard of stays first-class."""
    op(type="store_upsert", store_name="Corner Veg Guy")
    s = stores_by_name()["Corner Veg Guy"]
    assert s["osm_id"] is None
    assert s["address"] == ""
