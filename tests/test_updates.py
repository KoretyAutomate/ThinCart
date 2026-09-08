"""The in-app update channel, /version and /apk.

Nothing here builds an APK. What is pinned: nothing published is a 404 on both
routes, a published build is served with its metadata and the CORS header the
launcher needs, and the metadata can never point the file route outside DIST.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault(
    "THINCART_DB",
    str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db"),
)
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from fastapi.testclient import TestClient

import app as appmod
import updates

client = TestClient(appmod.app)


@pytest.fixture
def dist(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "DIST", tmp_path)
    return tmp_path


def test_nothing_published_is_a_404_on_both_routes(dist):
    assert client.get("/version").status_code == 404
    assert client.get("/apk").status_code == 404


def test_a_published_build_is_served_with_its_metadata(dist):
    (dist / "thincart.apk").write_bytes(b"PK\x03\x04 not really")
    meta = {"versionCode": 3, "versionName": "1.2", "file": "thincart.apk", "sha256": "ab", "size": 19}
    (dist / "version.json").write_text(json.dumps(meta))
    v = client.get("/version")
    assert v.status_code == 200 and v.json()["versionCode"] == 3
    # The launcher reads this from Capacitor's own origin.
    assert v.headers["access-control-allow-origin"] == "*"
    assert v.headers["cache-control"] == "no-cache"
    a = client.get("/apk")
    assert a.status_code == 200
    assert a.headers["content-type"].startswith("application/vnd.android.package-archive")
    assert a.content == b"PK\x03\x04 not really"


def test_metadata_naming_a_missing_file_is_a_404(dist):
    (dist / "version.json").write_text(json.dumps({"file": "gone.apk"}))
    assert client.get("/version").status_code == 404
    assert client.get("/apk").status_code == 404


def test_the_file_route_never_leaves_dist(dist, tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("no")
    (dist / "version.json").write_text(json.dumps({"file": "../secret.txt"}))
    assert client.get("/apk").status_code == 404


def test_unreadable_metadata_is_a_500_not_a_traceback(dist):
    (dist / "version.json").write_text("{not json")
    assert client.get("/version").status_code == 500
    assert client.get("/apk").status_code == 500
