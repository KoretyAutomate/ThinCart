"""
cycles.py — purchase-cycle estimation + due scoring (pure functions, no LLM, no I/O).

PLAN.md §Intelligence layer 1:
- events <1 day apart coalesce into one (burst buys must not crush the median)
- median of successive intervals, needs ≥3 events (≥2 intervals)
- suggest when 0.85 ≤ days_since_last / median ≤ 3.0 (upper cap retires
  lapsed/seasonal items instead of nagging forever)
"""
from datetime import datetime, UTC
from statistics import median

MIN_EVENTS = 3
COALESCE_DAYS = 1.0
DUE_MIN = 0.85
DUE_MAX = 3.0

BINS = [  # (max_median_days, label)
    (4.5, "twice a week"),
    (9.0, "weekly"),
    (18.0, "bi-weekly"),
    (45.0, "monthly"),
    (float("inf"), "occasional"),
]


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def coalesce(timestamps: list[str]) -> list[datetime]:
    """Sort and merge events closer than COALESCE_DAYS (keep the first of each burst)."""
    out: list[datetime] = []
    for dt in sorted(_parse(t) for t in timestamps):
        if not out or (dt - out[-1]).total_seconds() / 86400 >= COALESCE_DAYS:
            out.append(dt)
    return out


def median_interval_days(timestamps: list[str]) -> float | None:
    events = coalesce(timestamps)
    if len(events) < MIN_EVENTS:
        return None
    gaps = [
        # strict=False: events[1:] is deliberately one shorter — pairwise gaps.
        (b - a).total_seconds() / 86400 for a, b in zip(events, events[1:], strict=False)
    ]
    return median(gaps)


def cycle_label(median_days: float) -> str:
    for cap, label in BINS:
        if median_days <= cap:
            return label
    raise AssertionError("unreachable")


def due_score(timestamps: list[str], now: datetime) -> float | None:
    """days_since_last / median_interval, or None if not enough history."""
    m = median_interval_days(timestamps)
    if not m or m <= 0:
        return None
    last = coalesce(timestamps)[-1]
    return (now - last).total_seconds() / 86400 / m


def suggest(history: dict[int, list[str]], now: datetime) -> list[dict]:
    """history: catalog_id -> purchase timestamps. Returns due items, most-due first.

    Caller filters out items already on the list and snoozed catalog rows —
    this function is deliberately blind to list/snooze state so it stays pure.
    """
    out = []
    for catalog_id, timestamps in history.items():
        score = due_score(timestamps, now)
        if score is None or not (DUE_MIN <= score <= DUE_MAX):
            continue
        m = median_interval_days(timestamps)
        out.append(
            {
                "catalog_id": catalog_id,
                "score": round(score, 2),
                "median_days": round(m, 1),
                "label": cycle_label(m),
            }
        )
    return sorted(out, key=lambda s: -s["score"])
