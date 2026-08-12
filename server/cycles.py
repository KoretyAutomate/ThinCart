"""
cycles.py — purchase-cycle estimation + due scoring (pure functions, no LLM, no I/O).

PLAN.md §Intelligence layer 1:
- events <1 day apart coalesce into one (burst buys must not crush the median)
- median of successive intervals, needs ≥3 events (≥2 intervals)
- suggest when 0.85 ≤ days_since_last / median ≤ 3.0 (upper cap retires
  lapsed/seasonal items instead of nagging forever)

PLAN.md §Intelligence layer 1b: every interval is measured in **in-town days**.
A week in Boston is not a week of milk, so the days the household was away are
subtracted from each gap (see `Away`). Passing no `Away` reproduces layer 1
exactly — the away set starts empty and this stays a refinement of the old
arithmetic rather than a replacement for it.

PLAN.md §Intelligence layer 1c: EVERY item the household has ever bought is in
scope, and cycles are grouped in **whole weeks** — weekly, bi-weekly, every 3
weeks, and onward without a ceiling. Shopping happens on a weekly rhythm, so
"every 3 weeks" is a sentence about this household; "monthly" was a bin that
quietly merged 3-week and 6-week items.

The week is for grouping and display ONLY. Due-scoring always uses the measured
interval in days, so an item that really runs at 17 days is judged at 17 days
and not at the 14 or 21 its label rounds to.

Two tiers, and they answer a SHOPPING question rather than a statistical one:

  HIGH      — due now, on a rhythm worth trusting. Buy it.
  POTENTIAL — might be needed before the week is out: either due now on shaky
              evidence, or not due yet but arriving within the coming week.

Tiering by estimator confidence alone was the wrong axis: "this estimate has a
wide spread" is not a thing anyone can act on in a supermarket aisle. Evidence
quality still decides which of the two an item lands in, but the tier itself is
about what to put in the basket.

"Within the week" is counted in the IN-TOWN days the next seven days actually
contain, so a week that is mostly a trip correctly needs almost nothing.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo, UTC
from statistics import median

from travel import HOME_TZ

MIN_EVENTS = 3  # purchases needed before a cycle is trusted enough to be HIGH
COALESCE_DAYS = 1.0
DUE_MIN = 0.85
DUE_MAX = 3.0

# Only the most recent gaps count. A household's rhythm drifts — a new job, a
# baby, summer — and an interval from four months ago is evidence about a
# routine that may no longer exist.
RECENT_INTERVALS = 4

# Below this the item is bought more than once a week, and no whole number of
# weeks describes it. 5.25 = 0.75 weeks, the midpoint that rounds to 1.
SUB_WEEKLY_DAYS = 5.25

HIGH = "high"
POTENTIAL = "potential"
NO_TIER = None  # tracked, but nothing to do about it this week

# How far ahead "this week" looks, in calendar days. Converted to in-town days
# against the calendar before use — a week with a four-day trip in it holds
# three days of shopping, and should ask for three days of groceries.
WEEK_HORIZON_DAYS = 7

# ...but the horizon alone cannot decide anything for an item whose cycle is
# already shorter than a week: milk bought yesterday is "due within 7 days" and
# would sit in POTENTIAL permanently, which for the 13 weekly items in this
# household is most of the list, most of the time. So an item must also be at
# least halfway through its cycle — before that, it demonstrably still has some.
POTENTIAL_MIN_SCORE = 0.5

# Consistency gate for trusting a rhythm: the spread of the recent gaps
# relative to their middle. At 1.0 the widest gap may be about double the
# narrowest. Looser than that is not a rhythm, it is a coincidence.
MAX_SPREAD_FOR_TRUST = 1.0

# A shaky estimate retires sooner. Nagging to 3× a number that is itself a
# coin-flip is how a widened scope turns into noise.
RETIRE_AT = {True: DUE_MAX, False: 2.0}  # keyed by `trusted`


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Away:
    """The days the household was out of town, as home-local calendar dates.

    Deducted from every interval the estimator measures. The subtraction is
    fractional: an away day costs only the part of itself that actually falls
    inside the interval, so a trip beginning at 15:00 removes 0.375 of that
    day rather than all of it. Rounding to whole days would be a >30 % error
    on a three-day gap — the exact size of gap this feature exists to fix.

    An empty Away is the identity: `between()` degrades to plain elapsed days.
    """

    __slots__ = ("days", "tz")

    def __init__(self, days: Iterable[date] = (), tz: tzinfo = HOME_TZ):
        self.days = frozenset(days)
        self.tz = tz

    def __bool__(self) -> bool:
        return bool(self.days)

    def _local_midnight(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.tz)

    def _span(self, a: datetime, b: datetime) -> float:
        """Days from a to b as the household's own clock counts them.

        Both ends are moved to home-local time and stripped of their offset
        before subtracting, so the arithmetic is wall-clock: the 23-hour day at
        a spring-forward and the 25-hour day at a fall-back are each one day.

        This matters because `bought_at` is stored in UTC while away days are
        local dates. Subtracting UTC instants measures absolute elapsed time,
        which on a DST-crossing interval disagrees with the whole local days
        being subtracted from it — an hour's worth of cycle appearing or
        vanishing twice a year, in a unit documented as local calendar days.
        """
        local_a = a.astimezone(self.tz).replace(tzinfo=None)
        local_b = b.astimezone(self.tz).replace(tzinfo=None)
        return (local_b - local_a).total_seconds() / 86400

    def overlap(self, a: datetime, b: datetime) -> float:
        """Days of [a, b) spent away, in home-local calendar days."""
        if not self.days or b <= a:
            return 0.0
        total = 0.0
        day = a.astimezone(self.tz).date()
        last = b.astimezone(self.tz).date()
        while day <= last:
            if day in self.days:
                lo = max(a, self._local_midnight(day))
                hi = min(b, self._local_midnight(day + timedelta(days=1)))
                if hi > lo:
                    total += self._span(lo, hi)
            day += timedelta(days=1)
        return total

    def between(self, a: datetime, b: datetime) -> float:
        """In-town days elapsed from a to b, never negative."""
        return max(self._span(a, b) - self.overlap(a, b), 0.0)

    def advance(self, start: datetime, town_days: float) -> datetime:
        """The wall-clock moment `town_days` in-town days after `start`.

        The inverse of `between`, and the reason snoozes survive a trip: a
        dismissal that lasts "half a cycle" must not quietly expire while the
        household is in Boston consuming nothing. Only *known* away days can be
        skipped, so this reaches as far ahead as the calendar sync does.

        Always returned in `start`'s own timezone. Walking local midnights
        otherwise hands back a home-local datetime, and callers serialise it
        next to UTC timestamps that are later compared as STRINGS — a stored
        "01:00-04:00" sorts below a "03:00+00:00" it actually follows, so a live
        snooze reads as expired for the width of the offset.
        """
        if not self.days or town_days <= 0:
            return start + timedelta(days=max(town_days, 0.0))
        cur, remaining = start, town_days
        tz_out = start.tzinfo
        for _ in range(3650):  # a decade of days: a bound, not an expected path
            if remaining <= 1e-9:
                break
            day = cur.astimezone(self.tz).date()
            next_midnight = self._local_midnight(day + timedelta(days=1))
            if day in self.days:
                cur = next_midnight  # away: burns no cycle
                continue
            chunk = self._span(cur, next_midnight)
            if chunk >= remaining:
                return (cur + timedelta(days=remaining)).astimezone(tz_out)
            remaining -= chunk
            cur = next_midnight
        return cur.astimezone(tz_out)


NO_TRAVEL = Away()


def coalesce(timestamps: list[str]) -> list[datetime]:
    """Sort and merge events closer than COALESCE_DAYS (keep the first of each burst)."""
    out: list[datetime] = []
    for dt in sorted(_parse(t) for t in timestamps):
        if not out or (dt - out[-1]).total_seconds() / 86400 >= COALESCE_DAYS:
            out.append(dt)
    return out


@dataclass(frozen=True)
class Estimate:
    """One item's measured rhythm, plus whether that rhythm is worth trusting.

    `trusted` is evidence quality — enough purchases, and gaps that agree. It
    is NOT the tier: it is one of the two inputs the tier is computed from, the
    other being how soon the item comes due. Confidence is what the app knows;
    the tier is what the household should do about it.
    """

    events: int                 # coalesced purchases = distinct shopping trips
    cycle_days: float | None    # measured interval; None when bought once
    spread: float | None        # (max-min)/middle over the recent gaps
    trusted: bool               # enough consistent history to act on confidently
    last: datetime | None

    @property
    def cycle(self) -> float | None:
        """The usable cycle, or None. Every caller that divides by a cycle goes
        through here: a plain `has_cycle` flag asserted the value was safe
        without producing it, which read fine but left both the type checker
        and the next reader to take it on trust."""
        return self.cycle_days if self.cycle_days and self.cycle_days > 0 else None

    @property
    def has_cycle(self) -> bool:
        return self.cycle is not None

    @property
    def weeks(self) -> int | None:
        """Whole weeks for grouping, or None when bought oftener than weekly."""
        cycle = self.cycle
        return None if cycle is None else cycle_weeks(cycle)

    def score(self, now: datetime, away: Away = NO_TRAVEL) -> float | None:
        """In-town days since the last buy, as a fraction of the measured cycle.

        Deliberately against the measured cycle, never the rounded week: an item
        on a 17-day rhythm is due at 17 days, not at the 14 its "bi-weekly"
        label would imply.
        """
        cycle = self.cycle
        if cycle is None or self.last is None:
            return None
        return away.between(self.last, now) / cycle

    def due_in_days(self, now: datetime, away: Away = NO_TRAVEL) -> float | None:
        """In-town days until this comes due; ≤0 means it already is.

        This is the number the week horizon is compared against, and the number
        worth showing: "due in 3 days" is actionable in a way that a 0.71 score
        never was.
        """
        cycle = self.cycle
        if cycle is None or self.last is None:
            return None
        return DUE_MIN * cycle - away.between(self.last, now)

    def retired(self, now: datetime, away: Away = NO_TRAVEL) -> bool:
        """So far overdue it has lapsed — a seasonal item, or one they stopped
        buying. Suggesting it forever is how a tray becomes wallpaper."""
        s = self.score(now, away)
        return s is not None and s > RETIRE_AT[self.trusted]

    def tier(self, now: datetime, away: Away = NO_TRAVEL) -> str | None:
        """What to do about this item, on a shopping trip taken now.

        HIGH      due now, on a rhythm worth trusting → buy it
        POTENTIAL due now but the rhythm is shaky, OR at least halfway through
                  its cycle and arriving within the in-town days the coming
                  week holds → consider it
        None      nothing to do this week
        """
        due_in = self.due_in_days(now, away)
        if due_in is None or self.retired(now, away):
            return None
        if due_in <= 0:
            return HIGH if self.trusted else POTENTIAL
        score = self.score(now, away) or 0.0
        coming_up = score >= POTENTIAL_MIN_SCORE and due_in <= week_horizon(now, away)
        return POTENTIAL if coming_up else None

    def is_due(self, now: datetime, away: Away = NO_TRAVEL) -> bool:
        """Actionable at all this week — either tier."""
        return self.tier(now, away) is not None


def week_horizon(now: datetime, away: Away = NO_TRAVEL) -> float:
    """How many in-town days the coming week actually contains.

    A week with a four-day trip in it holds three days of shopping, so it
    should pull three days of groceries forward, not seven. A week spent
    entirely away pulls nothing — which is the correct answer, and one the
    calendar link is what makes available.
    """
    return away.between(now, now + timedelta(days=WEEK_HORIZON_DAYS))


def _gaps(events: list[datetime], away: Away) -> list[float]:
    # strict=False: events[1:] is deliberately one shorter — pairwise gaps.
    return [away.between(a, b) for a, b in zip(events, events[1:], strict=False)]


def estimate(timestamps: list[str], away: Away = NO_TRAVEL) -> Estimate:
    """Everything the estimator can say about one item's purchase history.

    Only the most recent `RECENT_INTERVALS` gaps count, so a rhythm that
    changed three months ago stops arguing with the one in force now. Their
    middle is the cycle; their spread, together with how many purchases there
    are, decides whether the rhythm is `trusted`.

    A cycle of 0 (every recent gap swallowed by travel) leaves has_cycle False
    rather than dividing later: such an item has no rhythm to report, and an
    infinite score would pin it to the top of the tray forever.
    """
    events = coalesce(timestamps)
    last = events[-1] if events else None
    gaps = _gaps(events, away)
    if not gaps:
        return Estimate(len(events), None, None, False, last)

    recent = gaps[-RECENT_INTERVALS:]
    cycle = median(recent)
    spread = (max(recent) - min(recent)) / cycle if cycle > 0 else None
    trusted = len(events) >= MIN_EVENTS and spread is not None and spread <= MAX_SPREAD_FOR_TRUST
    return Estimate(len(events), cycle, spread, trusted, last)


def median_interval_days(timestamps: list[str], away: Away = NO_TRAVEL) -> float | None:
    """The measured cycle for a history with ≥3 purchases, else None.

    Deliberately blind to two-purchase histories: a caller that wants a
    single-gap number must ask for the `Estimate` and handle its tier, rather
    than receiving a float that looks like every other float.
    """
    est = estimate(timestamps, away)
    return est.cycle_days if est.events >= MIN_EVENTS else None


def cycle_weeks(cycle_days: float) -> int | None:
    """Whole weeks, or None for an item bought more often than once a week."""
    if cycle_days < SUB_WEEKLY_DAYS:
        return None
    return max(1, round(cycle_days / 7))


def cycle_label(cycle_days: float) -> str:
    """Canonical English label. The UI renders from `weeks` so it can translate
    an open-ended series; this exists for logs, tests and the API's own words."""
    weeks = cycle_weeks(cycle_days)
    if weeks is None:
        return "more than weekly"
    return {1: "weekly", 2: "bi-weekly"}.get(weeks, f"every {weeks} weeks")


def due_score(timestamps: list[str], now: datetime, away: Away = NO_TRAVEL) -> float | None:
    """in-town days since last / cycle length, across every tier that has one."""
    return estimate(timestamps, away).score(now, away)


def suggest(history: dict[int, list[str]], now: datetime, away: Away = NO_TRAVEL) -> list[dict]:
    """history: catalog_id -> purchase timestamps. Returns due items, most-due first.

    Returns both tiers: HIGH (due now, trusted rhythm) and POTENTIAL (due now
    on shaky evidence, or arriving within the week). An item bought once never
    appears — there is no interval for it to be overdue against. The tier rides
    along so the tray can mark a potential suggestion as the guess it is.

    Caller filters out items already on the list and snoozed catalog rows —
    this function is deliberately blind to list/snooze state so it stays pure.
    """
    out = []
    for catalog_id, timestamps in history.items():
        est = estimate(timestamps, away)
        tier = est.tier(now, away)
        cycle, score, due_in = est.cycle, est.score(now, away), est.due_in_days(now, away)
        # a tier implies all three exist; binding them first states that once,
        # instead of leaving each use to imply it
        if tier is None or cycle is None or score is None or due_in is None:
            continue
        out.append(
            {
                "catalog_id": catalog_id,
                "score": round(score, 2),
                "median_days": round(cycle, 1),
                "due_in_days": round(due_in, 1),
                "weeks": est.weeks,
                "label": cycle_label(cycle),
                "tier": tier,
                "events": est.events,
            }
        )
    # buy-now items first, then what is merely coming up, each most-due first
    return sorted(out, key=lambda s: (s["tier"] != HIGH, -s["score"]))
