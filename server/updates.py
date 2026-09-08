"""
updates.py — the in-app update channel: GET /version and GET /apk.

The Android shell is sideloaded — no Play, no review, no store account — and
until now every new build meant downloading a CI artifact and installing it by
hand. Android keeps app data across an update as long as the package name and
signing key are unchanged, which the CI cert-drift gate guarantees, so nothing
stood between the phone and a one-tap update except delivery. This is delivery.

Publish a build with:  python3 server/publish_apk.py <app-debug.apk>
(or server/publish_latest_apk.sh, which fetches the newest CI artifact first).
That writes dist/version.json and copies the APK beside it; /version serves the
metadata and /apk the file. The launcher page in the APK — the only page that
can reach the native installer — compares versionCode and offers the install.

Ported from OutfitAdvisor's updates.py, where this has worked since 2026-07.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger("thincart.updates")
router = APIRouter()

DIST = Path(os.environ.get("THINCART_DIST", Path(__file__).resolve().parent.parent / "dist"))


def _meta() -> dict:
    meta = DIST / "version.json"
    if not meta.is_file():
        raise HTTPException(status_code=404, detail="no build published")
    try:
        d = json.loads(meta.read_text())
    except Exception:
        log.warning("version.json is unreadable")
        raise HTTPException(status_code=500, detail="bad build metadata") from None
    if not isinstance(d, dict):
        raise HTTPException(status_code=500, detail="bad build metadata")
    return d


@router.get("/version")
async def version() -> JSONResponse:
    """Metadata for the currently published APK, or 404 when none is published.

    Answered with a permissive CORS header on purpose: the launcher page runs
    on Capacitor's own origin and reads this cross-origin. The body is public
    within the tailnet and names nothing about the household."""
    d = _meta()
    if not (DIST / str(d.get("file") or "")).is_file():
        log.warning("version.json points at a missing apk")
        raise HTTPException(status_code=404, detail="apk missing")
    return JSONResponse(d, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


@router.get("/apk")
async def apk() -> FileResponse:
    """The published APK. The app verifies the sha256 from /version before it
    hands the file to the system installer."""
    name = str(_meta().get("file") or "")
    # Never let the metadata escape DIST — it is local, but a path traversal here
    # would turn a config typo into an arbitrary-file read over the tailnet.
    path = (DIST / name).resolve()
    if not name or DIST.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="apk missing")
    log.info("apk served (%s)", name)
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=name)
