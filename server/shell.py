"""
shell.py — serving the page itself, and never a stale copy of it.

Small, but it earned its own file. For weeks a deploy would land, the tests
would pass, and the phone would keep drawing the previous version: `/`, `/sw.js`
and `/manifest.json` went out with no Cache-Control, so an HTTP cache fell back
to heuristic freshness and answered from its own store for hours. The service
worker did not rescue it — sw.js is network-first, but its fetch() goes through
that same cache, so "network-first" was first in line behind a hit.

Two halves, and the second matters as much as the first:

  - `no_stale_shell` makes those three paths revalidate. "no-cache" means ask,
    not do-not-store; with an ETag a current phone gets a 304 and a few bytes.
  - `index()` stamps the page with the hash of the file it came from, so a
    client can say which version it is holding. What let the staleness run for
    weeks was not the caching — it was that a stale shell is indistinguishable
    from a working one.
"""

import hashlib
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

APP_DIR = Path(__file__).parent.parent / "app"
# Replaced with build_id() as the page is served; the literal never reaches a phone.
BUILD_PLACEHOLDER = "__BUILD__"

# Files that must never be served stale. Without an explicit Cache-Control an
# HTTP cache falls back to HEURISTIC freshness — roughly a tenth of the age since
# Last-Modified — so a phone can hold yesterday's page for hours. That is the
# recurring "I deployed it and the phone shows the old one", and the service
# worker does NOT rescue it: sw.js is network-first, but its fetch() goes through
# the very cache that is answering stale.
#
# "no-cache" does not mean do-not-store; it means revalidate every time. With the
# ETag that FileResponse/StaticFiles already send, a fresh phone gets a 304 and a
# few bytes. Icons are left alone — they are content-addressed by name and change
# about never.
ALWAYS_FRESH = {"/", "/index.html", "/sw.js", "/manifest.json"}


async def no_stale_shell(request, call_next):
    response = await call_next(request)
    if request.url.path in ALWAYS_FRESH:
        response.headers["Cache-Control"] = "no-cache"
    return response


def build_id() -> str:
    """Content hash of the page on disk — DERIVED, never hand-maintained.

    A constant someone has to remember to bump is worse than no stamp at all:
    the one time it is forgotten, a stale phone and the server report the same
    value and the staleness warning confidently says everything is current. The
    hash cannot be forgotten, because changing the file IS changing the hash.
    """
    try:
        return hashlib.sha256((APP_DIR / "index.html").read_bytes()).hexdigest()[:8]
    except OSError:
        return "unknown"


@router.get("/")
@router.get("/index.html")
async def index(request: Request):
    """The page, stamped with the hash of the file it was built from.

    Serving it through a template substitution rather than straight off disk is
    what makes the stamp trustworthy: a phone holding an old shell is holding an
    old hash, so the mismatch it reports is a fact about the bytes it has rather
    than about a constant someone may have forgotten to change.

    ETag is that same hash, so an up-to-date phone still gets a 304 and pays
    nothing for the freshness check.

    /index.html is routed here as well. Left to the StaticFiles mount it would
    serve the source untouched, placeholder and all, and every such client would
    be told forever that it is out of date — a warning that is always wrong is
    worse than none, because you learn to ignore it.
    """
    bid = build_id()
    etag = f'"{bid}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    body = (APP_DIR / "index.html").read_text().replace(BUILD_PLACEHOLDER, bid)
    return HTMLResponse(body, headers=headers)
