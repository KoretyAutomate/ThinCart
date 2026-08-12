"""
away.py — the Travel feature's HTTP surface and its calendar poller.

PLAN.md §Intelligence layer 1b. Split out of app.py because it is a whole
self-contained feature — a poller, three endpoints and a review model — and
because app.py had grown past the size the repo's quality ceiling allows.

Shared server state (the SQLite connection, the write lock, the broadcast) is
handed over by `bind()` at startup rather than imported from app.py, which
would be circular. Nothing here touches those objects before binding.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import calendar_sync
import db
import travel

log = logging.getLogger("thincart.away")
router = APIRouter()

SYNC_EVERY_H = 6

# Last sync outcome, surfaced in the Travel panel. A calendar that silently
# stopped syncing would leave cycles drifting with nothing on screen to say why.
state: dict = {"at": None, "error": None, "detected": 0}


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


async def sync_calendar() -> dict:
    """Pull the calendar window, write away-day proposals, tell the phones.

    Detection only ever proposes: `record_away_candidates` will not overwrite a
    day the user has already confirmed or rejected, and pruning is limited to
    unreviewed calendar rows.
    """
    ctx = _need()
    now = datetime.now(UTC)
    time_min = now - timedelta(days=calendar_sync.WINDOW_BACK_DAYS)
    time_max = now + timedelta(days=calendar_sync.WINDOW_AHEAD_DAYS)
    events = await asyncio.to_thread(calendar_sync.fetch_events, None, time_min, time_max)
    found = travel.detect(events)

    # away_days is keyed by HOME-LOCAL dates, so the pruning window has to be
    # expressed in them too. Taking .date() off the UTC bounds shifts the window
    # by a day whenever the two calendars disagree — after 20:00 in New York —
    # and a proposal sitting on that boundary escapes pruning, outliving the
    # calendar event that produced it.
    window = (
        time_min.astimezone(travel.HOME_TZ).date().isoformat(),
        time_max.astimezone(travel.HOME_TZ).date().isoformat(),
    )
    async with ctx.write_lock:
        ts = ctx.now_iso()
        db.record_away_candidates(ctx.conn, found, ts)
        dropped = db.prune_away_candidates(
            ctx.conn,
            *window,
            {c.day.isoformat() for c in found},
        )
        db.bump_revision(ctx.conn)
        ctx.conn.commit()
    state.update(at=ts, error=None, detected=len(found))
    log.info("calendar sync: %d events, %d away days, %d stale dropped", len(events), len(found), dropped)
    await ctx.broadcast()
    return {"events": len(events), "away_days": len(found), "dropped": dropped, "at": ts}


async def sweeper() -> None:
    """Poll the calendar. A failure is logged and retried next cycle — the
    shopping list keeps working with the away days it already has."""
    while True:
        if calendar_sync.is_linked():
            try:
                await sync_calendar()
            except Exception as exc:
                state.update(error=str(exc))
                log.warning("calendar sync failed: %s", exc)
        await asyncio.sleep(SYNC_EVERY_H * 3600)


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
        "linked": calendar_sync.is_linked(),
        "timezone": str(travel.HOME_TZ),
        "last_sync": state["at"],
        "last_error": state["error"],
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


@router.post("/api/calendar/sync")
async def post_calendar_sync():
    """Sync now, instead of waiting for the 6-hourly poll."""
    if not calendar_sync.is_linked():
        raise HTTPException(400, "no calendar linked — run calendar_sync.py --authorize on the server")
    try:
        return await sync_calendar()
    except calendar_sync.CalendarError as exc:
        state.update(error=str(exc))
        raise HTTPException(502, str(exc)) from exc
