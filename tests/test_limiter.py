"""Auth rate limiter pins — no such test existed before Phase E (the E1 gate
'rate limits survived the merge' was vacuous without one)."""
import uuid

from fastapi.testclient import TestClient

import app as appmod  # noqa: E402
import config  # noqa: E402

client = TestClient(appmod.app)


def _attempt(headers=None):
    return client.post("/api/auth/login", json={
        "email": f"nobody-{uuid.uuid4().hex[:6]}@example.com",
        "password": "wrong-password"}, headers=headers or {})


def test_auth_hammering_hits_429():
    appmod._auth_hits.clear()
    for _ in range(appmod.AUTH_MAX):
        assert _attempt().status_code == 401
    assert _attempt().status_code == 429


def test_flag_off_ignores_fly_client_ip_header():
    """Off-Fly the header is client-forgeable: rotating it must NOT mint fresh
    buckets (that would be an unlimited brute-force bypass)."""
    appmod._auth_hits.clear()
    assert config.TRUST_FLY_CLIENT_IP is False
    for i in range(appmod.AUTH_MAX):
        _attempt(headers={"Fly-Client-IP": f"10.0.0.{i}"})
    assert _attempt().status_code == 429


def test_flag_on_buckets_per_fly_client_ip_and_falls_back(monkeypatch):
    monkeypatch.setattr(config, "TRUST_FLY_CLIENT_IP", True)
    appmod._auth_hits.clear()
    for _ in range(appmod.AUTH_MAX):
        _attempt(headers={"Fly-Client-IP": "203.0.113.7"})
    # same forwarded IP → limited
    assert _attempt(headers={"Fly-Client-IP": "203.0.113.7"}).status_code == 429
    # different real client → its own bucket
    assert _attempt(headers={"Fly-Client-IP": "203.0.113.8"}).status_code == 401
    # header absent (fly proxy debugging, .internal) → peer-IP fallback, no error
    assert _attempt().status_code in (401, 429)


def test_spoofed_xff_never_influences_bucketing(monkeypatch):
    monkeypatch.setattr(config, "TRUST_FLY_CLIENT_IP", True)
    appmod._auth_hits.clear()
    for i in range(appmod.AUTH_MAX):
        _attempt(headers={"X-Forwarded-For": f"198.51.100.{i}"})
    # rotating XFF gained nothing: all landed in the peer bucket
    assert _attempt().status_code == 429


def test_idle_buckets_are_evicted(monkeypatch):
    appmod._auth_hits.clear()
    _attempt(headers={"Fly-Client-IP": "192.0.2.1"})
    # age every recorded hit past the window, then trigger any request
    for k in list(appmod._auth_hits):
        appmod._auth_hits[k] = [t - (appmod.AUTH_WINDOW_S + 1) for t in appmod._auth_hits[k]]
    _attempt()
    assert len(appmod._auth_hits) == 1  # only the fresh key survives
