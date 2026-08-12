"""
travel.py — which calendar days the household was out of town (pure, no I/O).

PLAN.md §Intelligence layer 1b. Google Calendar has no "this is travel" field,
so this is a heuristic that produces *proposals*: every day it returns is shown
in the Travel panel for the user to confirm or reject. That review step is what
buys the licence to guess — a wrong guess is visible and one tap from being
corrected, instead of silently bending every cycle in the app.

The rules, in the order they are applied to one event:
- an OUT_OF_OFFICE event is taken at its word;
- an all-day event spanning ≥2 days is travel (Gmail's auto-created
  "Stay at <hotel>" bookings are exactly this shape, which is how the real
  Jul 31–Aug 2 Boston trip is caught);
- a single all-day event is travel only if it *says* so (hotel/flight/trip);
- a timed event is never travel. An evening dinner reservation and a Saturday
  open house are both days at home, and treating them otherwise would erase
  more real days than the trips it caught.
"""

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# The household's home timezone: "in town" is measured in local calendar days,
# so a purchase at 21:00 EDT belongs to that day and not to the UTC tomorrow.
HOME_TZ = ZoneInfo(os.environ.get("THINCART_TZ", "America/New_York"))

# Comma-separated place words that mean "still home" — a hotel booking in your
# own town is not a trip. Empty by default: with review in front of every
# result, a false positive costs one tap, and guessing a home region from the
# calendar would cost real trips.
HOME_PATTERNS = tuple(
    p for p in (s.strip().casefold() for s in os.environ.get("THINCART_HOME_PATTERNS", "").split(",")) if p
)

MIN_MULTIDAY_SPAN = 2  # all-day events shorter than this must say they are travel

_TRAVEL_WORDS = re.compile(
    r"hotel|motel|inn\b|resort|airbnb|lodge|hostel|stay at|check[- ]?in"
    r"|flight|airline|airport|boarding|itinerary|layover"
    r"|trip|travel|vacation|getaway|cruise|conference in|offsite|off-site"
    r"|ホテル|旅館|宿泊|フライト|搭乗|出張|旅行|帰国|帰省|休暇",
    re.IGNORECASE,
)

# Recurring all-day events that span days without anybody leaving the house.
_NOT_TRAVEL_WORDS = re.compile(r"birthday|anniversary|payday|rent due|誕生日|記念日|給料日", re.IGNORECASE)

# Event types that are never travel regardless of shape. BIRTHDAY is an annual
# all-day event, so the multi-day rule would otherwise swallow it.
_IGNORED_EVENT_TYPES = frozenset({"BIRTHDAY", "WORKING_LOCATION"})


@dataclass(frozen=True)
class AwayCandidate:
    """One home-local day the calendar suggests was spent out of town."""

    day: date
    event_id: str
    summary: str
    location: str
    reason: str


def _fold(text: str) -> str:
    """NFKC fold so full-width ｈｏｔｅｌ and ホテル match the same patterns as ASCII."""
    return unicodedata.normalize("NFKC", text or "")


def _parse_day(value: str) -> date:
    """An all-day bound: '2026-07-31', or the timestamp form some clients return."""
    return date.fromisoformat(value[:10])


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=HOME_TZ)


def event_dates(event: dict, tz: ZoneInfo = HOME_TZ) -> list[date]:
    """The home-local dates an event covers.

    Google's all-day `end.date` is EXCLUSIVE — a hotel stay checking out on the
    3rd occupies the 31st, 1st and 2nd. Getting this wrong would charge the
    household a day of travel it spent at home, on every single trip.
    """
    start, end = event.get("start") or {}, event.get("end") or {}
    if "date" in start:
        first = _parse_day(start["date"])
        last = _parse_day(end["date"]) - timedelta(days=1) if "date" in end else first
        last = max(last, first)  # a zero-length all-day event still covers its day
    elif "dateTime" in start:
        first = _parse_dt(start["dateTime"]).astimezone(tz).date()
        if "dateTime" not in end:
            return [first]
        finish = _parse_dt(end["dateTime"]).astimezone(tz)
        # The interval is half-open, so an event finishing exactly at midnight
        # covers none of the day it lands on. An out-of-office running Monday
        # 09:00 to Tuesday 00:00 is one day away, not two — and the extra day
        # would be subtracted from every cycle once the trip was confirmed.
        last = finish.date() - timedelta(days=1) if finish.time() == time.min else finish.date()
        last = max(last, first)
    else:
        return []
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _is_all_day(event: dict) -> bool:
    return "date" in (event.get("start") or {})


def _declined(event: dict) -> bool:
    """An invitation the user turned down says nothing about where they were."""
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees") or [])


def _looks_local(text: str, home_patterns: tuple[str, ...]) -> bool:
    folded = _fold(text).casefold()
    return any(p in folded for p in home_patterns)


def classify(event: dict, home_patterns: tuple[str, ...] = HOME_PATTERNS) -> str | None:
    """Why this event means "out of town", or None if it does not.

    The returned string is shown to the user in the review list, so it has to
    name the rule that fired — "3-day all-day event" is reviewable, "true" is not.
    """
    if event.get("status") == "cancelled" or _declined(event):
        return None
    event_type = event.get("eventType", "DEFAULT")
    if event_type in _IGNORED_EVENT_TYPES:
        return None

    text = f"{_fold(event.get('summary', ''))} {_fold(event.get('location', ''))}"
    if _NOT_TRAVEL_WORDS.search(text):
        return None
    if _looks_local(text, home_patterns):
        return None

    if event_type == "OUT_OF_OFFICE":
        return "marked out of office"
    if not _is_all_day(event):
        return None  # timed events are days at home — see the module docstring

    span = len(event_dates(event))
    if span >= MIN_MULTIDAY_SPAN:
        return f"{span}-day all-day event"
    if _TRAVEL_WORDS.search(text):
        return "travel booking"
    return None


def detect(
    events: list[dict],
    tz: ZoneInfo = HOME_TZ,
    home_patterns: tuple[str, ...] = HOME_PATTERNS,
) -> list[AwayCandidate]:
    """Away-day proposals from a batch of calendar events, earliest day first.

    One day can be covered by two events (a hotel stay and the conference in
    it); the first event to claim a day wins, so the day is proposed once.
    """
    seen: dict[date, AwayCandidate] = {}
    for event in events:
        reason = classify(event, home_patterns)
        if reason is None:
            continue
        for day in event_dates(event, tz):
            seen.setdefault(
                day,
                AwayCandidate(
                    day=day,
                    event_id=event.get("id", ""),
                    summary=(event.get("summary") or "").strip(),
                    location=(event.get("location") or "").strip(),
                    reason=reason,
                ),
            )
    return [seen[d] for d in sorted(seen)]


def group_trips(candidates: list[AwayCandidate]) -> list[dict]:
    """Collapse consecutive away days into trips, for a review list that reads
    as "Boston, 3 days" rather than three separate checkboxes."""
    trips: list[dict] = []
    for c in candidates:
        if trips and c.event_id == trips[-1]["event_id"] and c.day - trips[-1]["end"] == timedelta(days=1):
            trips[-1]["end"] = c.day
            trips[-1]["days"].append(c.day)
            continue
        trips.append(
            {
                "start": c.day,
                "end": c.day,
                "days": [c.day],
                "event_id": c.event_id,
                "summary": c.summary,
                "location": c.location,
                "reason": c.reason,
            }
        )
    return trips
