"""
Multi-tenant contract tests (branch `saas`). Exercised through the real HTTP
layer via TestClient — auth, household isolation, and the op semantics, with the
review's must-fix isolation findings each pinned by a test.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"saas_test_{uuid.uuid4().hex}.db"),
)
os.environ.setdefault("THINCART_SECRET", "test-secret")
os.environ.setdefault("THINCART_LLM_PROVIDER", "none")
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import seed_catalog  # noqa: E402

client = TestClient(appmod.app)
_n = 0


@pytest.fixture(autouse=True)
def _reset_inmemory_state():
    """Clear per-process state between tests. The auth rate limiter in particular
    would 429 later tests (TestClient shares one client IP across the whole run)."""
    appmod._auth_hits.clear()
    appmod.ideas_cache.clear()
    appmod._ws_tickets.clear()
    appmod.rooms.clear()
    yield


def register(name="U"):
    """Create a fresh account+household, return (token, household, user_id)."""
    global _n
    _n += 1
    r = client.post("/api/auth/register", json={
        "email": f"u{_n}_{uuid.uuid4().hex[:6]}@example.com",
        "password": "password123", "display_name": name})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["token"], d["household"], d["user_id"]


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def op(token, **fields):
    body = {"op_id": str(uuid.uuid4()), "actor": "t", **fields}
    return client.post("/api/op", json=body, headers=hdr(token)), body


def names(token):
    return sorted(i["name"] for i in client.get("/api/state", headers=hdr(token)).json()["items"])


# ---------------- auth ----------------

def test_register_login_and_bad_password():
    r = client.post("/api/auth/register", json={
        "email": "auth@example.com", "password": "password123", "display_name": "A"})
    assert r.status_code == 200 and r.json()["token"]
    assert client.post("/api/auth/login", json={
        "email": "auth@example.com", "password": "password123"}).status_code == 200
    assert client.post("/api/auth/login", json={
        "email": "auth@example.com", "password": "wrong"}).status_code == 401


def test_unauthenticated_endpoints_401():
    for path in ("/api/state", "/api/catalog", "/api/cycles"):
        assert client.get(path).status_code == 401
    assert client.post("/api/op", json={"op_id": "x"*10, "type": "add", "name": "z"}).status_code == 401


def test_duplicate_email_rejected():
    client.post("/api/auth/register", json={"email": "dup@example.com", "password": "password123"})
    assert client.post("/api/auth/register", json={
        "email": "dup@example.com", "password": "password123"}).status_code == 409


# ---------------- isolation (the core must-fixes) ----------------

def test_two_households_lists_are_isolated():
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    op(ta, type="add", name="AliceMilk", item_id=str(uuid.uuid4()))
    op(tb, type="add", name="BobBeer", item_id=str(uuid.uuid4()))
    assert "AliceMilk" in names(ta) and "AliceMilk" not in names(tb)
    assert "BobBeer" in names(tb) and "BobBeer" not in names(ta)


def test_join_by_code_shares_one_list():
    ta, hh, _ = register("A")
    op(ta, type="add", name="SharedThing", item_id=str(uuid.uuid4()))
    # spouse registers then joins by code
    tw, _, _ = register("W")
    rj = client.post("/api/households/join",
                     json={"invite_code": hh["invite_code"]}, headers=hdr(tw))
    assert rj.status_code == 200
    tw2 = rj.json()["token"]
    assert "SharedThing" in names(tw2)
    # spouse checks it off → original member sees it gone
    item_id = client.get("/api/state", headers=hdr(tw2)).json()["items"][0]["id"]
    op(tw2, type="checkoff", item_id=item_id)
    assert "SharedThing" not in names(ta)


def test_catalog_frequency_ranking_is_household_scoped():
    """Finding #4: /api/catalog COUNT must not leak/rank by other households' buys."""
    seed_catalog.seed(appmod.conn)
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    # A buys 食パン 3x; it must top A's ranking but NOT B's
    for _ in range(3):
        iid = str(uuid.uuid4())
        op(ta, type="add", name="食パン", item_id=iid)
        op(ta, type="checkoff", item_id=iid)
    cat_a = client.get("/api/catalog", headers=hdr(ta)).json()["catalog"]
    cat_b = client.get("/api/catalog", headers=hdr(tb)).json()["catalog"]
    assert cat_a[0]["name"] == "食パン"
    assert cat_b[0]["name"] != "食パン"  # B never bought it → not ranked up for B


def test_cycles_and_plants_scoped():
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    cid = db.get_or_create_catalog(appmod.conn, "scoped_apple")
    appmod.conn.execute("UPDATE item_catalog SET plants_json=?, is_edible=1 WHERE id=?",
                        ('["apple"]', cid))
    # only A has purchases of it
    from datetime import datetime, timedelta, timezone
    for d in (21, 14, 7, 1):
        appmod.conn.execute(
            "INSERT INTO purchase_events(household_id, catalog_id, bought_at) VALUES(?,?,?)",
            (_hid(ta), cid,
             (datetime.now(timezone.utc) - timedelta(days=d)).isoformat(timespec="seconds")))
    appmod.conn.commit()
    a_cyc = {c["name"] for c in client.get("/api/cycles", headers=hdr(ta)).json()["cycles"]}
    b_cyc = {c["name"] for c in client.get("/api/cycles", headers=hdr(tb)).json()["cycles"]}
    assert "scoped_apple" in a_cyc and "scoped_apple" not in b_cyc
    assert "apple" in client.get("/api/state", headers=hdr(ta)).json()["plants"]["week"]
    assert "apple" not in client.get("/api/state", headers=hdr(tb)).json()["plants"]["week"]


def _hid(token):
    import auth
    return auth.decode_token(token)["hh"]


# ---------------- op semantics (per household) ----------------

def test_op_replay_scoped_single_event():
    ta, _, _ = register("A")
    iid = str(uuid.uuid4())
    op(ta, type="add", name="eggs2", item_id=iid)
    body = {"op_id": str(uuid.uuid4()), "actor": "t", "type": "checkoff", "item_id": iid}
    r1 = client.post("/api/op", json=body, headers=hdr(ta))
    r2 = client.post("/api/op", json=body, headers=hdr(ta))
    assert r2.json()["replayed"] is True
    n = appmod.conn.execute(
        "SELECT COUNT(*) FROM purchase_events e JOIN item_catalog c ON c.id=e.catalog_id "
        "WHERE c.canonical_name=? AND e.household_id=?",
        (db.canonical("eggs2"), _hid(ta))).fetchone()[0]
    assert n == 1


def test_skip_no_event_checkoff_event():
    ta, _, _ = register("A")
    i1, i2 = str(uuid.uuid4()), str(uuid.uuid4())
    op(ta, type="add", name="skipme", item_id=i1)
    op(ta, type="skip", item_id=i1)
    op(ta, type="add", name="buyme", item_id=i2)
    op(ta, type="checkoff", item_id=i2)
    hid = _hid(ta)
    ev = lambda nm: appmod.conn.execute(
        "SELECT COUNT(*) FROM purchase_events e JOIN item_catalog c ON c.id=e.catalog_id "
        "WHERE c.canonical_name=? AND e.household_id=?", (db.canonical(nm), hid)).fetchone()[0]
    assert ev("skipme") == 0 and ev("buyme") == 1


def test_undo_cannot_cross_households():
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    iid = str(uuid.uuid4())
    op(ta, type="add", name="undoitem", item_id=iid)
    _, co = op(ta, type="checkoff", item_id=iid)
    # B tries to undo A's checkoff op_id → no-op (scoped ledger)
    r, _ = op(tb, type="undo_checkoff", target_op_id=co["op_id"])
    assert r.json()["result"] == {"noop": True}
    # A can undo its own
    r2, _ = op(ta, type="undo_checkoff", target_op_id=co["op_id"])
    assert "item_id" in r2.json()["result"]


# ---------------- account deletion ----------------

def test_account_delete_purges_and_revokes_token():
    ta, _, _ = register("Solo")
    op(ta, type="add", name="tofu9", item_id=str(uuid.uuid4()))
    hid = _hid(ta)
    assert client.post("/api/account/delete", headers=hdr(ta)).status_code == 204
    assert client.get("/api/state", headers=hdr(ta)).status_code == 401  # token dead
    # household-scoped rows gone
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM items WHERE household_id=?", (hid,)).fetchone()[0] == 0
    assert appmod.conn.execute(
        "SELECT COUNT(*) FROM households WHERE id=?", (hid,)).fetchone()[0] == 0


def test_delete_keeps_household_for_remaining_member():
    ta, hh, _ = register("Owner")
    tw, _, _ = register("Spouse")
    tw = client.post("/api/households/join",
                     json={"invite_code": hh["invite_code"]}, headers=hdr(tw)).json()["token"]
    op(ta, type="add", name="keepme", item_id=str(uuid.uuid4()))
    # owner deletes; spouse remains and still sees the shared list
    client.post("/api/account/delete", headers=hdr(ta))
    assert "keepme" in names(tw)


# ---------------- ideas cache isolation (mock LLM) ----------------

def test_ideas_cache_is_per_household(monkeypatch):
    async def fake(prompt, **kw):
        # tag the recipe with which household's "on_list" leaked into the prompt
        if "recipes" in prompt:
            return {"recipes": [{"title": "R", "uses": [], "missing": [], "new_plants": []}]}
        return {"suggestions": []}
    monkeypatch.setattr(llm, "chat_json", fake)
    ta, hha, _ = register("A")
    tb, hhb, _ = register("B")
    # ideas are plus-tier (Phase E entitlement gating); this test is about the
    # CACHE, so both households get the tier
    appmod.conn.execute("UPDATE households SET tier='plus' WHERE id IN (?,?)",
                        (hha["id"], hhb["id"]))
    appmod.conn.commit()
    da = client.get("/api/ideas?refresh=1", headers=hdr(ta)).json()
    # B's cache must be independent — not served A's entry
    assert appmod.ideas_cache.get(_hid(ta)) is not None
    assert appmod.ideas_cache.get(_hid(tb)) is None
    db_ = client.get("/api/ideas?refresh=1", headers=hdr(tb)).json()
    assert appmod.ideas_cache.get(_hid(tb)) is not None


# ---------------- WS room isolation ----------------

def test_ws_requires_ticket_and_is_room_scoped():
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    # no ticket → closed
    with pytest.raises(Exception):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    # A connects with a valid ticket, sees only its own state
    ticket = client.post("/api/ws-ticket", headers=hdr(ta)).json()["ticket"]
    with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
        first = ws.receive_json()
        assert "items" in first
        # B mutates → A's socket must NOT receive B's data
        op(tb, type="add", name="BsecretWS", item_id=str(uuid.uuid4()))
        op(ta, type="add", name="AownWS", item_id=str(uuid.uuid4()))
        pushed = ws.receive_json()  # A's own op triggers A's room broadcast
        nm = [i["name"] for i in pushed["items"]]
        assert "AownWS" in nm and "BsecretWS" not in nm


# ---------------- cross-tenant enrichment safety ----------------

def test_enrichment_merge_cannot_repoint_other_household(monkeypatch):
    """Finding #5: an alias_of verdict must not repoint a catalog row that a
    DIFFERENT household already has events for."""
    import asyncio
    ta, _, _ = register("A")
    tb, _, _ = register("B")
    # both households buy the same fresh user-typed item "gribble"
    for t in (ta, tb):
        iid = str(uuid.uuid4())
        op(t, type="add", name="gribble", item_id=iid)
        op(t, type="checkoff", item_id=iid)
    cid = db.get_or_create_catalog(appmod.conn, "gribble")
    # a real target to merge into
    target = db.get_or_create_catalog(appmod.conn, "gribbleberry")
    appmod.conn.execute("UPDATE item_catalog SET category='produce', llm_enriched_at='x' WHERE id=?",
                        (target,))
    appmod.conn.commit()

    async def fake(prompt, **kw):
        return {"is_real_item": True, "category": "produce", "is_edible": True,
                "plants": ["gribble"], "english_name": "gribble",
                "alias_of": db.canonical("gribbleberry")}
    monkeypatch.setattr(llm, "chat_json", fake)
    async def no_web(name):
        return None
    monkeypatch.setattr(catalog, "web_evidence", no_web)

    asyncio.run(catalog.enrich(appmod.conn, appmod.write_lock, cid))
    # the row must SURVIVE (two households touch it) — no destructive cross-tenant merge
    assert appmod.conn.execute("SELECT COUNT(*) FROM item_catalog WHERE id=?", (cid,)).fetchone()[0] == 1
