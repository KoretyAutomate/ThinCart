"""The Pixel app's calendar push (PLAN.md §2026-09-26).

The phone reads the calendar Android already syncs and POSTs the all-day events
to /api/calendar/events in the Google shape `travel.detect` takes. These pin the
server half: proposals land for review, pruning is bounded to what the phone
could have read, an empty read never wipes the review queue, and the last sync
survives a restart.
"""

import importlib
import sys
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import away


def _window(now=None):
    now = now or datetime.now(UTC)
    return {
        "start": (now - timedelta(days=away.WINDOW_BACK_DAYS)).isoformat(),
        "end": (now + timedelta(days=away.WINDOW_AHEAD_DAYS)).isoformat(),
    }


def _trip(first: date, nights: int = 3, eid: str = "hotel:1") -> dict:
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
    monkeypatch.setenv("THINCART_DB", str(tmp_path / "phone.db"))
    import app as appmod

    importlib.reload(appmod)
    return TestClient(appmod.app)


def _push(client, events, calendars=1, window=None):
    return client.post(
        "/api/calendar/events",
        json={"window": window or _window(), "calendars": calendars, "events": events},
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
    r = _push(client, [], calendars=0)
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
    now = datetime.now(UTC)
    old = (now - timedelta(days=away.WINDOW_BACK_DAYS + 30)).date()
    _push(client, [_trip(old)], window={"start": (now - timedelta(days=390)).isoformat(), "end": now.isoformat()})
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

    importlib.reload(appmod)  # same THINCART_DB, fresh process state
    assert TestClient(appmod.app).get("/api/away").json()["last_sync"]["away_days"] == 3
