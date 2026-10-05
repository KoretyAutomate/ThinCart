"""The Pixel app's calendar push (PLAN.md §2026-09-26).

The phone reads the calendar Android already syncs and POSTs the all-day events
to /api/calendar/events in the Google shape `travel.detect` takes. These pin the
server half: proposals land for review, pruning is bounded to what the phone
could have read, an empty read never wipes the review queue, and the last sync
survives a restart.
"""

import importlib
import os
import sys
import uuid
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Like every other server-importing test module: point THINCART_DB at a
# throwaway file BEFORE the first import, or `import away` → `import db` freezes
# DB_PATH onto the live household DB when this file runs on its own.
os.environ["THINCART_DB"] = str(Path(os.environ.get("PYTEST_TMP", "/tmp")) / f"thincart_test_{uuid.uuid4().hex}.db")
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import away_db

import away


def _window(now=None):
    now = now or datetime.now(UTC)
    return {
        "start": (now - timedelta(days=away.WINDOW_BACK_DAYS)).isoformat(),
        "end": (now + timedelta(days=away.WINDOW_AHEAD_DAYS)).isoformat(),
    }


def _trip(first: date, nights: int = 3, eid: str = "7:1:0") -> dict:
    """A Gmail-style hotel stay, as the Pixel sends it: all-day, end exclusive."""
    return {
        "id": eid,
        "summary": "Stay at Hotel AKA Boston Common",
        "location": "Boston",
        "start": {"date": first.isoformat()},
        "end": {"date": (first + timedelta(days=nights)).isoformat()},
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A throwaway DB, bound BEFORE the app connects. `import away` above has
    already imported `db`, freezing DB_PATH and connect()'s default at whatever
    THINCART_DB was then — the live household DB when this file runs alone.
    Reloading only `app` would reuse that; reloading `db` first re-reads it."""
    monkeypatch.setenv("THINCART_DB", str(tmp_path / "phone.db"))
    import db as dbmod

    importlib.reload(dbmod)  # before app is first imported, so it never opens another DB
    import app as appmod

    importlib.reload(appmod)
    assert Path(appmod.conn.execute("PRAGMA database_list").fetchone()[2]) == tmp_path / "phone.db"
    return TestClient(appmod.app)


def _push(client, events, calendars=("7",), window=None):
    return client.post(
        "/api/calendar/events",
        json={"window": window or _window(), "calendars": list(calendars), "events": events},
    )


def _pending_days(client) -> list[str]:
    return [d for t in client.get("/api/away").json()["trips"] if t["pending"] for d in t["days"]]


def test_a_pushed_trip_becomes_a_proposal_and_the_sync_is_recorded(client):
    first = date.today() - timedelta(days=20)
    r = _push(client, [_trip(first)])
    assert r.status_code == 200, r.text
    assert r.json()["away_days"] == 3
    assert _pending_days(client) == [(first + timedelta(days=n)).isoformat() for n in range(3)]
    last = client.get("/api/away").json()["last_sync"]
    assert last["calendars"] == 1 and last["events"] == 1 and last["away_days"] == 3


def test_a_trip_gone_from_the_calendar_is_pruned(client):
    _push(client, [_trip(date.today() - timedelta(days=20))])
    r = _push(client, [])
    assert r.json()["dropped"] == 3
    assert _pending_days(client) == []


def test_reading_zero_calendars_never_wipes_the_review_queue(client):
    """Access revoked or the calendar hidden looks like 'no events' — it is not
    'every trip was cancelled'."""
    _push(client, [_trip(date.today() - timedelta(days=20))])
    r = _push(client, [], calendars=())
    assert r.status_code == 200 and r.json()["dropped"] == 0
    assert len(_pending_days(client)) == 3
    assert client.get("/api/away").json()["last_sync"]["calendars"] == 0


def test_a_reviewed_day_survives_the_event_disappearing(client):
    first = date.today() - timedelta(days=20)
    _push(client, [_trip(first)])
    client.post("/api/away", json={"day": first.isoformat(), "status": "confirmed"})
    _push(client, [])
    rows = {t["start"]: t for t in client.get("/api/away").json()["trips"]}
    assert first.isoformat() in rows and rows[first.isoformat()]["pending"] is False


def test_pruning_stays_inside_the_servers_own_window(client):
    """A phone posting a year-wide window must not prune proposals older than
    anything the server would have read itself."""
    import app as appmod

    now = datetime.now(UTC)
    old = (now - timedelta(days=away.WINDOW_BACK_DAYS + 30)).date()
    # an older proposal, from a sync made months ago when this day was in range
    away_db.record_away_candidates(
        appmod.conn,
        [away.travel.AwayCandidate(old + timedelta(days=n), "7:old:0", "Stay", "", "3-day all-day event")
         for n in range(3)],
        "2026-01-01T00:00:00+00:00",
    )
    appmod.conn.commit()
    assert len(_pending_days(client)) == 3
    r = _push(client, [], window={"start": (now - timedelta(days=390)).isoformat(), "end": now.isoformat()})
    assert r.json()["dropped"] == 0
    assert len(_pending_days(client)) == 3


@pytest.mark.parametrize(
    "window",
    [
        {"start": "2026-09-10T00:00:00+00:00", "end": "2026-09-01T00:00:00+00:00"},  # backwards
        {"start": "2020-01-01T00:00:00+00:00", "end": "2020-02-01T00:00:00+00:00"},  # nowhere near now
        {"start": "2026-09-01T00:00:00", "end": "2026-09-10T00:00:00"},  # no timezone
    ],
)
def test_implausible_windows_are_refused(client, window):
    assert _push(client, [], window=window).status_code == 422


def test_an_oversized_push_is_refused(client):
    trip = _trip(date.today())
    assert _push(client, [trip] * (away.MAX_EVENTS + 1)).status_code == 422


def test_clean_event_keeps_only_what_the_detector_reads():
    raw = {
        "id": "x",
        "summary": "s" * 1000,
        "location": 42,
        "start": {"date": "2026-08-01", "junk": "y"},
        "end": {"date": "2026-08-03"},
        "status": "cancelled",
        "attendees": [{"self": True, "responseStatus": "declined", "email": "a@b"}],
        "description": "private notes",
    }
    ev = away.clean_event(raw)
    assert set(ev) == {"id", "summary", "location", "start", "end", "status", "attendees"}
    assert len(ev["summary"]) == away.MAX_TEXT and ev["location"] == ""
    assert ev["start"] == {"date": "2026-08-01"}
    assert ev["attendees"] == [{"self": True, "responseStatus": "declined"}]


def test_last_sync_survives_a_restart(client, tmp_path):
    _push(client, [_trip(date.today() - timedelta(days=20))])
    import app as appmod
    import db as dbmod

    importlib.reload(dbmod)
    importlib.reload(appmod)  # same THINCART_DB, fresh process state
    assert Path(appmod.conn.execute("PRAGMA database_list").fetchone()[2]) == tmp_path / "phone.db"
    assert TestClient(appmod.app).get("/api/away").json()["last_sync"]["away_days"] == 3


def test_an_endless_event_is_dropped_not_expanded(client):
    """Codex review 2026-09-26: one event spanning 1900→2100 inside a valid
    window was expanded into 73,049 proposals."""
    endless = {"id": "7:x:0", "summary": "trip", "start": {"date": "1900-01-01"}, "end": {"date": "2100-01-01"}}
    r = _push(client, [endless])
    assert r.status_code == 200 and r.json()["away_days"] == 0
    assert _pending_days(client) == []


def test_only_days_inside_the_window_are_recorded(client):
    """A trip that starts before the window is proposed for its in-window days only."""
    now = datetime.now(UTC)
    edge = (now - timedelta(days=away.WINDOW_BACK_DAYS + 2)).date()
    r = _push(client, [_trip(edge, nights=6)])
    assert r.status_code == 200
    days = _pending_days(client)
    window_start = (now - timedelta(days=away.WINDOW_BACK_DAYS)).astimezone(away.travel.HOME_TZ).date()
    assert days and min(days) >= window_start.isoformat()
    assert len(days) < 6


def test_a_calendar_not_read_this_time_keeps_its_trips(client):
    """Codex review 2026-09-26: two accounts on the Pixel, one calendar hidden
    — its trips were pruned though nothing about them changed."""
    a, b = date.today() - timedelta(days=40), date.today() - timedelta(days=20)
    _push(client, [_trip(a, eid="7:1:0"), _trip(b, eid="9:2:0")], calendars=("7", "9"))
    assert len(_pending_days(client)) == 6
    r = _push(client, [_trip(a, eid="7:1:0")], calendars=("7",))  # calendar 9 hidden
    assert r.json()["dropped"] == 0
    assert len(_pending_days(client)) == 6


def test_an_event_cannot_speak_for_a_calendar_that_was_not_read(client):
    r = _push(client, [_trip(date.today() - timedelta(days=20), eid="99:1:0")], calendars=("7",))
    assert r.json()["events"] == 0 and _pending_days(client) == []


def test_a_boundary_day_the_phone_only_partly_read_is_not_pruned(client):
    """Codex review 2026-09-26: on a New York evening the window's first local
    date is only partly covered by the phone's read."""
    import app as appmod

    now = datetime.now(UTC)
    first = (now - timedelta(days=away.WINDOW_BACK_DAYS)).astimezone(away.travel.HOME_TZ).date()
    edge = away.travel.AwayCandidate(first, "7:edge:0", "Stay", "", "travel booking")
    away_db.record_away_candidates(appmod.conn, [edge], "2026-01-01T00:00:00+00:00")
    appmod.conn.commit()
    r = _push(client, [])
    assert r.json()["dropped"] == 0
    assert first.isoformat() in _pending_days(client)


def test_a_day_two_calendars_share_survives_one_going_quiet(client):
    """Codex review 2026-09-26: the same trip on calendars 7 and 9; 9 is then
    hidden and 7's event deleted. 9 still claims the days — they must stay."""
    first = date.today() - timedelta(days=20)
    _push(client, [_trip(first, eid="7:1:0"), _trip(first, eid="9:2:0")], calendars=("7", "9"))
    assert len(_pending_days(client)) == 3
    r = _push(client, [], calendars=("7",))
    assert r.json()["dropped"] == 0
    assert len(_pending_days(client)) == 3
    # and once 9 is read again without the trip, nothing claims them
    r = _push(client, [], calendars=("7", "9"))
    assert r.json()["dropped"] == 3


def test_a_legacy_google_proposal_is_reconciled_by_a_phone_read(client):
    """Codex review 2026-09-26: rows from the old Google pull have Google event
    ids, which name no phone calendar — they must not become unprunable."""
    import app as appmod

    first = date.today() - timedelta(days=30)
    legacy = away.travel.AwayCandidate(first, "lj2rnl3q6knq6ustu8tqj495s0", "Stay", "", "3-day all-day event")
    away_db.record_away_candidates(appmod.conn, [legacy], "2026-08-11T00:00:00+00:00")
    appmod.conn.commit()
    assert _push(client, []).json()["dropped"] == 1
    assert _pending_days(client) == []
