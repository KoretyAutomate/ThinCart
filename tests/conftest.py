"""
Shared test wiring for the multi-tenant suite.

One pytest process = one app instance = one throwaway DB (PLANTCART_DB is set
here, BEFORE any test module imports `app`). Isolation between test modules
comes from multi-tenancy itself: each module registers its own household, so
cross-file interference is impossible by the same mechanism that isolates
real customers.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault(
    "PLANTCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"saas_test_{uuid.uuid4().hex}.db"),
)
os.environ.setdefault("PLANTCART_SECRET", "test-secret")
os.environ.setdefault("PLANTCART_LLM_PROVIDER", "none")
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


@pytest.fixture(autouse=True)
def _reset_inmemory_state():
    """Clear per-process state between tests. The auth rate limiter in
    particular would 429 later tests (TestClient shares one client IP)."""
    import app as appmod

    appmod._auth_hits.clear()
    appmod.ideas_cache.clear()
    appmod._ws_tickets.clear()
    appmod.rooms.clear()
    yield


_n = 0


def register_household(client, name="U"):
    """Fresh account + household → (token, household, user_id)."""
    global _n
    _n += 1
    r = client.post("/api/auth/register", json={
        "email": f"conf{_n}_{uuid.uuid4().hex[:6]}@example.com",
        "password": "password123", "display_name": name})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["token"], d["household"], d["user_id"]


def auth_hdr(token):
    return {"Authorization": f"Bearer {token}"}
