"""Away-day detection and in-town cycle math (PLAN.md §Intelligence layer 1b).

The Boston fixtures are the real events from the calendar this feature was
built against — a Gmail-generated hotel stay over 2026-07-31→08-02, and the
Sunday open house on 08-09 that must NOT read as travel.
"""

import sys
from datetime import date, datetime, time, timedelta, UTC
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import cycles
import travel

NY = ZoneInfo("America/New_York")

BOSTON = {
    "id": "lj2rnl3q6knq6ustu8tqj495s0",
    "summary": "Stay at Hotel AKA Boston Common",
    "location": "90 Tremont Street, Boston, Massachusetts 02108, United States",
    "eventType": "FROM_GMAIL",
    "status": "confirmed",
    "start": {"date": "2026-07-31"},
    "end": {"date": "2026-08-03"},
}

OPEN_HOUSE = {
    "id": "c8rj6o9n60pjcb9nccr6ab9k",
    "summary": "Open House: 614 Parker Blvd, Princeton Junction, NJ 08550",
    "location": "614 Parker Blvd, Princeton Junction, NJ 08550",
    "eventType": "DEFAULT",
    "start": {"dateTime": "2026-08-09T14:00:00-04:00"},
    "end": {"dateTime": "2026-08-09T16:00:00-04:00"},
}

DINNER = {
    "id": "1b9s0b4seis2qomhu6qhb0rl0g",
    "summary": "Reservation at Ma Maison",
    "eventType": "FROM_GMAIL",
    "start": {"dateTime": "2026-08-01T18:00:00-04:00"},
    "end": {"dateTime": "2026-08-01T19:00:00-04:00"},
}


# ------------------------------------------------------------------ detection


def test_hotel_stay_covers_nights_not_checkout_day():
    """Google's all-day end is EXCLUSIVE: checking out on the 3rd means the
    31st, 1st and 2nd were away — charging the 3rd too would invent a day."""
    assert travel.event_dates(BOSTON) == [date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)]
    assert travel.classify(BOSTON) == "3-day all-day event"


def test_timed_events_are_days_at_home():
    """A Saturday open house and a dinner reservation are not trips. This is
    the rule that keeps detection from erasing ordinary days."""
    assert travel.classify(OPEN_HOUSE) is None
    assert travel.classify(DINNER) is None


def test_single_all_day_needs_travel_wording():
    plain = {"id": "a", "summary": "Trash day", "start": {"date": "2026-08-05"}, "end": {"date": "2026-08-06"}}
    flight = {"id": "b", "summary": "Flight to Tokyo", "start": {"date": "2026-08-05"}, "end": {"date": "2026-08-06"}}
    assert travel.classify(plain) is None
    assert travel.classify(flight) == "travel booking"


def test_japanese_travel_wording_matches():
    ev = {"id": "c", "summary": "出張（大阪）", "start": {"date": "2026-08-05"}, "end": {"date": "2026-08-06"}}
    assert travel.classify(ev) == "travel booking"


def test_out_of_office_is_taken_at_its_word():
    ev = {
        "id": "d",
        "summary": "OOO",
        "eventType": "OUT_OF_OFFICE",
        "start": {"dateTime": "2026-08-05T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-05T17:00:00-04:00"},
    }
    assert travel.classify(ev) == "marked out of office"


def test_birthdays_and_declined_invites_are_not_travel():
    birthday = {
        "id": "e",
        "summary": "Jenni's birthday",
        "eventType": "BIRTHDAY",
        "start": {"date": "2026-08-05"},
        "end": {"date": "2026-08-08"},
    }
    declined = {
        "id": "f",
        "summary": "Team offsite",
        "start": {"date": "2026-08-05"},
        "end": {"date": "2026-08-08"},
        "attendees": [{"self": True, "responseStatus": "declined"}],
    }
    assert travel.classify(birthday) is None
    assert travel.classify(declined) is None


def test_home_patterns_suppress_a_local_booking():
    local = dict(BOSTON, location="12 Nassau St, Princeton, NJ")
    assert travel.classify(local, home_patterns=("princeton",)) is None
    assert travel.classify(local) is not None  # only suppressed when configured


def test_detect_dedupes_overlapping_events_and_groups_trips():
    conference = {
        "id": "g",
        "summary": "Conference in Boston",
        "start": {"date": "2026-08-01"},
        "end": {"date": "2026-08-03"},
    }
    found = travel.detect([BOSTON, conference, OPEN_HOUSE, DINNER])
    assert [c.day for c in found] == [date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)]
    trips = travel.group_trips(found)
    assert len(trips) == 1
    assert trips[0]["start"] == date(2026, 7, 31) and trips[0]["end"] == date(2026, 8, 2)
    assert trips[0]["summary"] == "Stay at Hotel AKA Boston Common"


# --------------------------------------------------------------- in-town math


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=NY) if day <= 31 else datetime(2026, 8, day - 31, hour, tzinfo=NY)


BOSTON_DAYS = [date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)]


def test_between_subtracts_only_the_away_days():
    away = cycles.Away(BOSTON_DAYS, NY)
    a, b = at(29), at(35)  # Jul 29 12:00 → Aug 4 12:00 = 6 calendar days
    assert (b - a).total_seconds() / 86400 == 6.0
    assert away.between(a, b) == 3.0


def test_partial_days_are_charged_fractionally():
    """A trip starting mid-afternoon costs a fraction of that day, not all of
    it — whole-day rounding is a >30 % error on the gaps this feature targets."""
    away = cycles.Away([date(2026, 8, 1)], NY)
    a = datetime(2026, 8, 1, 18, tzinfo=NY)  # 6 h of the away day remain
    b = datetime(2026, 8, 2, 0, tzinfo=NY)
    assert abs(away.overlap(a, b) - 0.25) < 1e-9
    assert abs(away.between(a, b)) < 1e-9


def test_empty_away_is_the_identity():
    """Layer 1b must be a refinement: with nothing recorded, the arithmetic is
    bit-for-bit the old calendar-day behaviour."""
    hist = [(datetime(2026, 6, 1, 10, tzinfo=UTC) + timedelta(days=d)).isoformat() for d in (0, 7, 14, 21)]
    assert cycles.median_interval_days(hist, cycles.Away()) == cycles.median_interval_days(hist)


def test_travel_shortens_the_learned_cycle():
    """The whole point: a household that shops weekly but spent 3 days away
    inside one of those gaps consumes on a shorter in-town cycle than the
    calendar suggests."""
    hist = [
        datetime(2026, 7, 22, 12, tzinfo=NY).isoformat(),
        datetime(2026, 7, 29, 12, tzinfo=NY).isoformat(),
        datetime(2026, 8, 5, 12, tzinfo=NY).isoformat(),  # spans the Boston trip
    ]
    plain = cycles.median_interval_days(hist)
    in_town = cycles.median_interval_days(hist, cycles.Away(BOSTON_DAYS, NY))
    assert plain == 7.0
    assert in_town == 5.5  # gaps 7 and 4 → median 5.5
    assert cycles.cycle_label(in_town) == "weekly"


def test_one_stray_trip_is_still_absorbed_by_the_median():
    """Layer 1b does not make the median fragile. With enough clean gaps around
    it, a single trip still moves nothing — this feature earns its keep on a
    household that travels repeatedly, not on one weekend in Boston."""
    hist = [
        datetime(2026, 7, 22, 12, tzinfo=NY).isoformat(),
        datetime(2026, 7, 29, 12, tzinfo=NY).isoformat(),
        datetime(2026, 8, 5, 12, tzinfo=NY).isoformat(),
        datetime(2026, 8, 12, 12, tzinfo=NY).isoformat(),
    ]
    # gaps 7, 4, 7 → median still 7
    assert cycles.median_interval_days(hist, cycles.Away(BOSTON_DAYS, NY)) == 7.0


def test_due_score_uses_in_town_days_since_last():
    hist = [
        datetime(2026, 7, 10, 12, tzinfo=NY).isoformat(),
        datetime(2026, 7, 17, 12, tzinfo=NY).isoformat(),
        datetime(2026, 7, 24, 12, tzinfo=NY).isoformat(),
        datetime(2026, 7, 30, 12, tzinfo=NY).isoformat(),
    ]
    now = datetime(2026, 8, 4, 12, tzinfo=NY)  # 5 calendar days, 2 in town
    away = cycles.Away(BOSTON_DAYS, NY)
    plain = cycles.due_score(hist, now)
    travelled = cycles.due_score(hist, now, away)
    assert plain > travelled
    # 3 of the 5 elapsed days were spent in Boston, so only 2 count
    assert abs(travelled * cycles.median_interval_days(hist, away) - 2.0) < 1e-9


def test_all_travel_interval_disables_rather_than_dividing_by_zero():
    """A history whose every gap is travel has no cycle to report; it must not
    produce an infinite score that pins the item to the top of the tray."""
    away = cycles.Away([date(2026, 8, 1) + timedelta(days=n) for n in range(10)], NY)
    hist = [datetime(2026, 8, d, 12, tzinfo=NY).isoformat() for d in (2, 4, 6, 8)]
    assert cycles.median_interval_days(hist, away) == 0.0
    assert cycles.due_score(hist, datetime(2026, 8, 20, 12, tzinfo=NY), away) is None
    assert cycles.suggest({1: hist}, datetime(2026, 8, 20, 12, tzinfo=NY), away) == []


def test_advance_skips_travel_so_a_snooze_survives_a_trip():
    """A dismissal lasting 2 in-town days, taken the day before leaving, must
    still be live when the household gets home."""
    away = cycles.Away(BOSTON_DAYS, NY)
    start = datetime(2026, 7, 30, 12, tzinfo=NY)
    got = away.advance(start, 2.0)
    assert got == datetime(2026, 8, 4, 12, tzinfo=NY)  # 30th→31st, then 3 days skipped
    assert abs(away.between(start, got) - 2.0) < 1e-9


def test_advance_without_travel_is_plain_addition():
    start = datetime(2026, 7, 30, 12, tzinfo=NY)
    assert cycles.Away().advance(start, 3.5) == start + timedelta(days=3.5)


# ------------------------------------------------------------- DST integrity
# Codex review 2026-08-11: `bought_at` is stored in UTC while away days are
# local dates, so subtracting instants measured absolute time while the away
# days subtracted from it were whole local days. On a DST-crossing interval the
# two disagreed by an hour, in a unit documented as local calendar days.

DST_SPRING = date(2026, 3, 8)   # 23-hour local day
DST_FALL = date(2026, 11, 1)    # 25-hour local day


def utc(y, m, d, h=12):
    """A local wall-clock moment, stored the way bought_at stores it."""
    return datetime(y, m, d, h, tzinfo=NY).astimezone(UTC)


def test_ten_local_days_minus_three_away_is_seven_across_spring_forward():
    away = cycles.Away([date(2026, 3, 7), DST_SPRING, date(2026, 3, 9)], NY)
    assert away.between(utc(2026, 3, 5), utc(2026, 3, 15)) == 7.0


def test_ten_local_days_minus_three_away_is_seven_across_fall_back():
    away = cycles.Away([date(2026, 10, 31), DST_FALL, date(2026, 11, 2)], NY)
    assert away.between(utc(2026, 10, 29), utc(2026, 11, 8)) == 7.0


def test_a_dst_day_spent_away_costs_exactly_one_day():
    """23 and 25 hours are both one day off the cycle — the household did not
    consume more groceries because the clocks went back."""
    for day in (DST_SPRING, DST_FALL):
        away = cycles.Away([day], NY)
        start = datetime.combine(day, time.min, tzinfo=NY)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=NY)
        assert away.overlap(start, end) == 1.0


def test_plain_elapsed_is_unaffected_by_a_dst_crossing():
    """With no away days the span is still whole calendar days, so a cycle
    learned across March does not drift against one learned in June."""
    assert cycles.Away().between(utc(2026, 3, 5), utc(2026, 3, 15)) == 10.0
    assert cycles.Away().between(utc(2026, 6, 5), utc(2026, 6, 15)) == 10.0


def test_advance_inverts_between_across_both_transitions():
    """A snooze deadline must land exactly where between() says it should."""
    for first in (date(2026, 3, 6), date(2026, 10, 30)):
        away = cycles.Away([first + timedelta(days=n) for n in range(3)], NY)
        start = datetime.combine(first, time.min, tzinfo=NY) + timedelta(hours=9)
        for want in (0.5, 2.0, 5.25):
            assert abs(away.between(start, away.advance(start, want)) - want) < 1e-9


def test_unreviewed_detections_do_not_touch_the_cycles(tmp_path):
    """Codex review 2026-08-11 (P1): an 'auto' row is a proposal. If it counted,
    review would be cosmetic — the first sync ingests 180 days of heuristics at
    once, so one bad match would reshape every cycle before anyone saw it."""
    import os

    os.environ["THINCART_DB"] = str(tmp_path / "t.db")
    import importlib

    import db as dbmod

    importlib.reload(dbmod)
    conn = dbmod.connect(tmp_path / "t.db")

    proposal = travel.AwayCandidate(
        day=date(2026, 8, 1), event_id="x", summary="Stay at Hotel", location="", reason="3-day all-day event"
    )
    dbmod.record_away_candidates(conn, [proposal], "2026-08-11T00:00:00+00:00")
    conn.commit()
    assert dbmod.away_rows(conn)[0]["status"] == "auto"
    assert dbmod.away_set(conn).days == frozenset()  # visible for review, inert

    dbmod.set_away_status(conn, "2026-08-01", "confirmed", "2026-08-11T00:00:00+00:00")
    conn.commit()
    assert dbmod.away_set(conn).days == frozenset({date(2026, 8, 1)})

    dbmod.set_away_status(conn, "2026-08-01", "rejected", "2026-08-11T00:00:00+00:00")
    conn.commit()
    assert dbmod.away_set(conn).days == frozenset()


def test_a_day_marked_away_by_hand_counts_at_once():
    """Manual entry is a decision, not a proposal — no second confirmation."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_schema())
    db_module().set_away_status(conn, "2026-08-01", "confirmed", "2026-08-11T00:00:00+00:00")
    rows = db_module().away_rows(conn)
    assert rows[0]["source"] == "manual" and rows[0]["status"] == "confirmed"
    assert db_module().away_set(conn).days == frozenset({date(2026, 8, 1)})


def db_module():
    import db as dbmod

    return dbmod


def db_schema() -> str:
    return db_module().SCHEMA


def test_timed_event_ending_at_midnight_does_not_claim_the_next_day():
    """Codex review 2026-08-11 (P2): the interval is half-open. An OOO running
    Mon 09:00 -> Tue 00:00 is one day away; counting Tuesday would subtract a
    day nobody spent travelling from every cycle once the trip was confirmed."""
    ooo = {
        "id": "x",
        "summary": "OOO",
        "eventType": "OUT_OF_OFFICE",
        "start": {"dateTime": "2026-08-03T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-04T00:00:00-04:00"},
    }
    assert travel.classify(ooo) == "marked out of office"
    assert travel.event_dates(ooo) == [date(2026, 8, 3)]


def test_timed_event_ending_just_after_midnight_does_claim_it():
    """A minute past midnight is a minute of that day, so it still counts —
    only the exact boundary is excluded."""
    ooo = {
        "id": "y",
        "summary": "OOO",
        "eventType": "OUT_OF_OFFICE",
        "start": {"dateTime": "2026-08-03T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-04T00:01:00-04:00"},
    }
    assert travel.event_dates(ooo) == [date(2026, 8, 3), date(2026, 8, 4)]


def test_timed_event_within_one_day_is_unaffected():
    ooo = {
        "id": "z",
        "summary": "OOO",
        "eventType": "OUT_OF_OFFICE",
        "start": {"dateTime": "2026-08-03T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-03T17:00:00-04:00"},
    }
    assert travel.event_dates(ooo) == [date(2026, 8, 3)]


def test_api_refuses_to_un_review_a_day(tmp_path, monkeypatch):
    """Codex review 2026-08-11 (P2): 'auto' is detection's internal state. Over
    the wire it would let a client reset a reviewed day to unreviewed — undoing
    the one guarantee this feature makes — or forge a calendar-looking row."""
    monkeypatch.setenv("THINCART_DB", str(tmp_path / "api.db"))
    import importlib

    from fastapi.testclient import TestClient

    import app as appmod

    importlib.reload(appmod)
    client = TestClient(appmod.app)

    assert client.post("/api/away", json={"day": "2026-08-01", "status": "confirmed"}).status_code == 200
    assert client.post("/api/away", json={"day": "2026-08-01", "status": "rejected"}).status_code == 200
    assert client.post("/api/away", json={"day": "2026-08-01", "status": "auto"}).status_code == 422
    assert client.post("/api/away", json={"day": "not-a-date", "status": "confirmed"}).status_code == 422


def test_advance_returns_the_timezone_it_was_given():
    """Codex review 2026-08-11 (P1): snooze deadlines are stored as ISO strings
    and compared LEXICALLY against UTC. A home-local return value serialises
    with a -04:00 offset that sorts below a +00:00 string it actually follows,
    so a live snooze reads as expired."""
    away = cycles.Away([date(2026, 8, 12), date(2026, 8, 13)], NY)
    start = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)
    got = away.advance(start, 2.0)
    assert got.tzinfo is UTC
    assert got.isoformat().endswith("+00:00")
    # and the instant is still right after the round trip
    assert abs(away.between(start, got) - 2.0) < 1e-9


def test_snooze_deadline_is_stored_comparable_to_now(tmp_path, monkeypatch):
    """The end-to-end shape of the same bug: what apply_snooze writes must sort
    correctly against the now_iso() strings the expiry checks use."""
    monkeypatch.setenv("THINCART_DB", str(tmp_path / "snz.db"))
    import importlib
    import uuid as _uuid

    from fastapi.testclient import TestClient

    import app as appmod

    importlib.reload(appmod)
    client = TestClient(appmod.app)

    cid = appmod.db.get_or_create_catalog(appmod.conn, "milk")
    for day in (1, 8, 15):
        appmod.conn.execute(
            "INSERT INTO purchase_events(catalog_id, bought_at) VALUES(?,?)",
            (cid, datetime(2026, 7, day, 12, tzinfo=UTC).isoformat(timespec="seconds")),
        )
    appmod.conn.commit()
    client.post("/api/away", json={"day": "2026-08-12", "status": "confirmed"})

    r = client.post("/api/op", json={"op_id": str(_uuid.uuid4()), "type": "snooze", "catalog_id": cid})
    assert r.status_code == 200
    until = r.json()["result"]["snoozed_until"]
    assert until.endswith("+00:00"), f"stored offset must be UTC, got {until}"
    assert until > appmod.now_iso()  # sorts as still-active, and truly is


def test_prune_window_is_in_home_local_dates(tmp_path, monkeypatch):
    """Codex review 2026-08-11 (P2): away_days is keyed by home-local dates, so
    a UTC-derived window is off by a day whenever the two calendars disagree
    (after 20:00 in New York), letting a boundary proposal outlive its event."""
    monkeypatch.setenv("THINCART_DB", str(tmp_path / "prune.db"))
    import importlib

    import away as awaymod
    import db as dbmod

    importlib.reload(dbmod)
    conn = dbmod.connect(tmp_path / "prune.db")

    # 22:00 in New York on the 11th is already the 12th in UTC
    now = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)
    assert now.date() != now.astimezone(NY).date()

    stale = travel.AwayCandidate(
        day=now.astimezone(NY).date(),
        event_id="gone",
        summary="deleted trip",
        location="",
        reason="3-day all-day event",
    )
    dbmod.record_away_candidates(conn, [stale], "2026-08-12T02:00:00+00:00")
    conn.commit()
    assert len(dbmod.away_rows(conn)) == 1

    # the window a sync computes, expressed the way the rows are keyed
    time_max = now + timedelta(days=awaymod.calendar_sync.WINDOW_AHEAD_DAYS)
    time_min = now - timedelta(days=awaymod.calendar_sync.WINDOW_BACK_DAYS)
    dropped = dbmod.prune_away_candidates(
        conn,
        time_min.astimezone(travel.HOME_TZ).date().isoformat(),
        time_max.astimezone(travel.HOME_TZ).date().isoformat(),
        set(),  # the calendar no longer claims any day
    )
    assert dropped == 1
    assert dbmod.away_rows(conn) == []
