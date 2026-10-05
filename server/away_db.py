"""
away_db.py — storage for the Travel feature's away days (split out of db.py).

PLAN.md §Intelligence layer 1b and §2026-09-26: proposals from the Pixel's
calendar, the owner's confirm/reject decisions, per-calendar claims, and the
set of confirmed days the cycle arithmetic subtracts.
"""

import json
import re
import sqlite3
from datetime import date


def away_set(conn: sqlite3.Connection):
    """The days that count as out of town — CONFIRMED ones only.

    Detection proposes; only a person decides (PLAN.md §1b). An 'auto' row is a
    heuristic guess awaiting review, and letting it into this set would make the
    review cosmetic: the first sync reads 180 days of calendar at once, so a
    single bad match — the real 12-day hotel booking in the household's OWN
    town — would silently reshape every cycle, every suggestion and every snooze
    deadline in the app before anyone had seen it. That is precisely the
    fully-automatic behaviour the review step exists to avoid.

    Manual entries are born 'confirmed', so marking a day away by hand counts
    immediately.
    """
    import cycles

    rows = conn.execute("SELECT day FROM away_days WHERE status = 'confirmed'")
    return cycles.Away(date.fromisoformat(r["day"]) for r in rows)


def away_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every away day, oldest first — the substrate for the Travel review panel."""
    return [dict(r) for r in conn.execute("SELECT * FROM away_days ORDER BY day")]


def record_away_candidates(conn: sqlite3.Connection, candidates, detected_at: str) -> int:
    """Upsert detected days, leaving the user's confirm/reject decisions alone.

    The WHERE on the DO UPDATE is the whole point: re-running a sync refreshes
    the event details behind a still-unreviewed 'auto' day, but a day the user
    has already ruled on is never touched. Without it every poll would quietly
    resurrect a trip the user had just rejected.
    """
    n = 0
    for c in candidates:
        cur = conn.execute(
            """INSERT INTO away_days(day, status, source, event_id, summary, location, reason, detected_at)
               VALUES(?, 'auto', 'calendar', ?, ?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                 event_id=excluded.event_id, summary=excluded.summary,
                 location=excluded.location, reason=excluded.reason,
                 detected_at=excluded.detected_at
               WHERE away_days.status='auto' AND away_days.source='calendar'""",
            (c.day.isoformat(), c.event_id, c.summary, c.location, c.reason, detected_at),
        )
        n += cur.rowcount
    return n


def prune_away_candidates(conn: sqlite3.Connection, start: str, end: str, keep: set) -> int:
    """Drop unreviewed calendar days in [start, end] the calendar no longer claims.

    A deleted or rescheduled trip has to stop counting, but only unreviewed
    ('auto') calendar rows are eligible — a manual entry or a confirmed day
    outlives whatever the calendar currently says.
    """
    stale = [
        r["day"]
        for r in conn.execute(
            """SELECT day FROM away_days
               WHERE status='auto' AND source='calendar' AND day BETWEEN ? AND ?""",
            (start, end),
        )
        if r["day"] not in keep
    ]
    conn.executemany("DELETE FROM away_days WHERE day=?", [(d,) for d in stale])
    return len(stale)


_PHONE_EVENT_ID = re.compile(r"^(\d+):\d+:-?\d+$")


def _claims_of(row) -> set[str] | None:
    """A row's claimant calendars. None for a LEGACY row — proposed by the old
    Google pull, whose event ids name no phone calendar — which any phone read
    may therefore speak for; otherwise it could never be pruned at all."""
    claims = set(json.loads(row["claims"] or "[]"))
    if claims:
        return claims
    m = _PHONE_EVENT_ID.match(row["event_id"] or "")
    return {m.group(1)} if m else None


def sync_away_claims(
    conn: sqlite3.Connection,
    claims_now: dict[str, set[str]],
    read: set[str],
    start: str,
    end: str,
) -> int:
    """Reconcile unreviewed calendar days with one phone read. Returns days dropped.

    For each 'auto' calendar row: the calendars read this time are replaced by
    what they claim now; calendars NOT read keep their earlier claim untouched.
    A day is dropped only when no calendar claims it any more — and only inside
    [start, end], the part of the window the read fully covered.

    Call after `record_away_candidates`, which creates the rows for new days.
    """
    dropped = 0
    rows = conn.execute("SELECT day, event_id, claims FROM away_days WHERE status='auto' AND source='calendar'")
    for r in list(rows):
        day = r["day"]
        now = claims_now.get(day, set())
        if not now and not (start <= day <= end):
            continue  # outside what was fully read: this read says nothing about it
        old = _claims_of(r)
        claims = now if old is None else (old - read) | now
        if claims:
            conn.execute("UPDATE away_days SET claims=? WHERE day=?", (json.dumps(sorted(claims)), day))
        else:
            conn.execute("DELETE FROM away_days WHERE day=?", (day,))
            dropped += 1
    return dropped


def set_away_status(conn: sqlite3.Connection, day: str, status: str, detected_at: str) -> dict:
    """Review action: confirm/reject a detected day, or mark one away by hand.

    A confirmed day with no calendar row behind it is a manual entry — the
    household travelled and the calendar never knew.
    """
    if status not in ("auto", "confirmed", "rejected"):
        raise ValueError(f"bad away status: {status}")
    date.fromisoformat(day)  # reject malformed keys before they reach the table
    cur = conn.execute("UPDATE away_days SET status=? WHERE day=?", (status, day))
    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO away_days(day, status, source, reason, detected_at)
               VALUES(?,?, 'manual', 'marked by hand', ?)""",
            (day, status, detected_at),
        )
    return {"day": day, "status": status}
