"""
away.py — the Travel feature's HTTP surface.

PLAN.md §Intelligence layer 1b, and §2026-09-26 for where the events come from:
the Pixel app reads the calendar Android already syncs and POSTs it here. There
is no Google client on the server any more — the OAuth app could not be
published, and a Testing-mode token dies every 7 days.

Split out of app.py because it is a whole self-contained feature — its
endpoints and a review model — and because app.py had grown past the size the
repo's quality ceiling allows.

Shared server state (the SQLite connection, the write lock, the broadcast) is
handed over by `bind()` at startup rather than imported from app.py, which
would be circular. Nothing here touches those objects before binding.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db
import travel

log = logging.getLogger("thincart.away")
router = APIRouter()

# How much calendar a sync covers. Purchase cycles are estimated from a few
# months of events at most, so reading further back would be waste. The phone
# asks for this window; the server clamps whatever it is sent to it.
WINDOW_BACK_DAYS = 180
WINDOW_AHEAD_DAYS = 30

# A posted window further out than this is not a clock skew, it is garbage.
WINDOW_SANITY_DAYS = 400

MAX_EVENTS = 5000
# The detector expands an event into every day it covers. An all-day event
# longer than this is not a trip (a "semester", a mis-set end year) — and one
# spanning 1900→2100 would be 73,000 proposals and a blocked event loop.
MAX_EVENT_DAYS = 120
MAX_TEXT = 300  # summary/location as shown in the Travel panel, never more

# Last sync, persisted so a restart does not make the Travel panel claim the
# calendar has never been read.
META_KEY = "calendar_sync"


@dataclass
class Context:
    """The server internals this feature borrows, injected at startup."""

    conn: sqlite3.Connection
    write_lock: asyncio.Lock
    broadcast: Callable[[], Awaitable[None]]
    now_iso: Callable[[], str]


_ctx: Context | None = None


def bind(ctx: Context) -> None:
    global _ctx
    _ctx = ctx


def _need() -> Context:
    if _ctx is None:  # pragma: no cover — a wiring error, not a runtime path
        raise RuntimeError("away.bind() was never called")
    return _ctx


class AwayOp(BaseModel):
    day: str = Field(..., min_length=10, max_length=10)  # YYYY-MM-DD, home-local

    # Decisions only. 'auto' is detection's own state and stays internal to it:
    # accepting it over the wire would let a client reset a reviewed day back to
    # unreviewed — breaking the one invariant this feature promises — and let a
    # hand-typed day masquerade as something the calendar proposed.
    status: Literal["confirmed", "rejected"]


class CalendarWindow(BaseModel):
    start: datetime
    end: datetime


class CalendarPush(BaseModel):
    """What the Pixel sends: the window it read, the calendars it read it from,
    and the events, already in the Google shape `travel.detect` takes. Each
    event id starts with its calendar's id: `<calendar>:<event>:<begin>`."""

    window: CalendarWindow
    calendars: list[str] = Field(..., max_length=50)
    events: list[dict] = Field(default_factory=list, max_length=MAX_EVENTS)


def _clip(value) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _bound(value) -> dict:
    """Only the two keys the detector reads, and only as short strings."""
    if not isinstance(value, dict):
        return {}
    return {k: value[k][:40] for k in ("date", "dateTime") if isinstance(value.get(k), str)}


def clean_event(raw: dict) -> dict:
    """Keep exactly the fields `travel.detect` reads. Anything else a client
    sends is dropped rather than trusted — this endpoint is open on the tailnet."""
    ev = {
        "id": _clip(raw.get("id")),
        "summary": _clip(raw.get("summary")),
        "location": _clip(raw.get("location")),
        "start": _bound(raw.get("start")),
        "end": _bound(raw.get("end")),
    }
    if raw.get("status") == "cancelled":
        ev["status"] = "cancelled"
    if any(isinstance(a, dict) and a.get("self") and a.get("responseStatus") == "declined"
           for a in raw.get("attendees") or []):
        ev["attendees"] = [{"self": True, "responseStatus": "declined"}]
    return ev


def _span_days(ev: dict) -> int | None:
    """How many days an event covers, without expanding it; None if unreadable."""
    try:
        start, end = ev["start"], ev["end"]
        if "date" in start:
            first = travel._parse_day(start["date"])
            last = travel._parse_day(end["date"]) if "date" in end else first
        else:
            first = travel._parse_dt(start["dateTime"]).date()
            last = travel._parse_dt(end["dateTime"]).date() if "dateTime" in end else first
    except (KeyError, ValueError, TypeError):
        return None
    return (last - first).days + 1


def bounded(events: list[dict]) -> list[dict]:
    """Events safe to hand to `travel.detect`: parseable, and not absurdly long."""
    out = []
    for ev in events:
        span = _span_days(ev)
        if span is not None and 0 < span <= MAX_EVENT_DAYS + 1:  # +1: an all-day end is exclusive
            out.append(ev)
    return out


def clamp_window(start: datetime, end: datetime, now: datetime) -> tuple[datetime, datetime]:
    """The posted window, checked and narrowed to the server's own.

    Pruning deletes unreviewed days the calendar no longer claims INSIDE the
    window, so a window wider than what was actually read would delete trips
    nobody looked for. Clamping to [now-180 d, now+30 d] means a phone can only
    ever prune what it could have read.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("window bounds need a timezone")
    if not start < end:
        raise ValueError("window start must be before its end")
    sanity = timedelta(days=WINDOW_SANITY_DAYS)
    if not (now - sanity <= start and end <= now + sanity):
        raise ValueError("window is outside any plausible calendar read")
    return (
        max(start, now - timedelta(days=WINDOW_BACK_DAYS)),
        min(end, now + timedelta(days=WINDOW_AHEAD_DAYS)),
    )


def last_sync(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


async def ingest(push: CalendarPush) -> dict:
    """Detect away days in what the phone read, write them as proposals, tell
    the phones.

    Detection only ever proposes: `record_away_candidates` will not overwrite a
    day the user has already confirmed or rejected, and pruning is limited to
    unreviewed calendar rows.
    """
    ctx = _need()
    now = datetime.now(UTC)
    time_min, time_max = clamp_window(push.window.start, push.window.end, now)
    read = {c[:64] for c in push.calendars if c}
    # an event may only speak for a calendar the phone says it read
    events = bounded(
        [
            clean_event(e)
            for e in push.events
            if isinstance(e, dict) and str(e.get("id", "")).split(":", 1)[0] in read
        ]
    )

    # away_days is keyed by HOME-LOCAL dates, so the pruning window has to be
    # expressed in them too. Taking .date() off the UTC bounds shifts the window
    # by a day whenever the two calendars disagree — after 20:00 in New York —
    # and a proposal sitting on that boundary escapes pruning, outliving the
    # calendar event that produced it.
    first = time_min.astimezone(travel.HOME_TZ).date()
    last = time_max.astimezone(travel.HOME_TZ).date()
    window = (first.isoformat(), last.isoformat())
    # Prune one day inside each edge. The phone's read starts at an instant and
    # the window at a home-local date; on a New York evening the first date is
    # only partly read, and an all-day trip ending on it would look cancelled.
    prune_window = ((first + timedelta(days=1)).isoformat(), (last - timedelta(days=1)).isoformat())
    # Record only what the window covers: a trip reaching past it is proposed
    # for its in-window days, and nothing outside is written that a later sync
    # could never prune.
    found = [c for c in travel.detect(events) if window[0] <= c.day.isoformat() <= window[1]]
    async with ctx.write_lock:
        ts = ctx.now_iso()
        dropped = 0
        # Zero calendars read means the phone saw nothing — access revoked,
        # sync switched off, the calendar hidden — not that every trip was
        # cancelled. Pruning on that would wipe every proposal awaiting review.
        if read:
            db.record_away_candidates(ctx.conn, found, ts)
            dropped = db.prune_away_candidates(
                ctx.conn, *prune_window, {c.day.isoformat() for c in found}, calendars=read
            )
        summary = {"at": ts, "calendars": len(read), "events": len(events), "away_days": len(found)}
        ctx.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_KEY, json.dumps(summary)),
        )
        db.bump_revision(ctx.conn)
        ctx.conn.commit()
    log.info(
        "calendar sync from phone: %d calendars, %d events, %d away days, %d stale dropped",
        len(read), len(events), len(found), dropped,
    )
    await ctx.broadcast()
    return {**summary, "dropped": dropped}


@router.get("/api/away")
async def get_away():
    """The Travel panel: detected trips awaiting review, plus link health."""
    conn = _need().conn
    rows = db.away_rows(conn)
    by_day = {
        date.fromisoformat(r["day"]): travel.AwayCandidate(
            day=date.fromisoformat(r["day"]),
            event_id=r["event_id"],
            summary=r["summary"],
            location=r["location"],
            reason=r["reason"],
        )
        for r in rows
        if r["status"] != "rejected"
    }
    status_of = {r["day"]: r["status"] for r in rows}
    trips = []
    for t in travel.group_trips([by_day[d] for d in sorted(by_day)]):
        days = [d.isoformat() for d in t["days"]]
        trips.append(
            {
                "start": t["start"].isoformat(),
                "end": t["end"].isoformat(),
                "days": days,
                "summary": t["summary"],
                "location": t["location"],
                "reason": t["reason"],
                # a trip is reviewed once every day in it has been ruled on
                "pending": any(status_of.get(d) == "auto" for d in days),
            }
        )
    return {
        "timezone": str(travel.HOME_TZ),
        # None until the Pixel app has read the calendar once
        "last_sync": last_sync(conn),
        "trips": trips,
        "rejected": [r["day"] for r in rows if r["status"] == "rejected"],
    }


@router.post("/api/away")
async def post_away(op: AwayOp):
    """Confirm or reject a detected day, or mark one away by hand.

    Whole-trip review is the client's job: it posts each day, so a partial trip
    (left Sunday, home Monday morning) stays expressible.
    """
    ctx = _need()
    async with ctx.write_lock:
        try:
            result = db.set_away_status(ctx.conn, op.day, op.status, ctx.now_iso())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        db.bump_revision(ctx.conn)
        ctx.conn.commit()
    await ctx.broadcast()  # cycles just changed on both phones
    return {"ok": True, **result, "revision": db.get_revision(ctx.conn)}


@router.post("/api/calendar/events")
async def post_calendar_events(push: CalendarPush):
    """The Pixel app's calendar read. Sent from native code, so no CORS."""
    try:
        return await ingest(push)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
