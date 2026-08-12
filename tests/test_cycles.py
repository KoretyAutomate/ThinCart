"""Interval-estimator tests with synthetic histories (PLAN.md Phase 1 gate)."""

import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import cycles

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def series(*day_offsets):
    return [(T0 + timedelta(days=d)).isoformat() for d in day_offsets]


def test_weekly_item():
    hist = series(0, 7, 14, 22, 28)  # weekly with jitter
    m = cycles.median_interval_days(hist)
    assert 6.5 <= m <= 7.5
    assert cycles.cycle_label(m) == "weekly"


def test_biweekly_with_one_skip():
    """One vacation skip (a 28d gap) must not drag a biweekly item to 4 weeks.

    Approximate, not exact: intervals are measured in home-local calendar days
    (see Away._span), and this fixture's last gap crosses the March DST
    transition, so it is legitimately 14 days and an hour. Pinning 14 exactly
    would be pinning the absence of a time change.
    """
    hist = series(0, 14, 28, 56, 70)  # 14,14,28,14 → median 14
    m = cycles.median_interval_days(hist)
    assert abs(m - 14) < 0.1
    assert cycles.cycle_label(m) == "bi-weekly"


def test_burst_coalescing():
    """Two checkoffs the same evening = one purchase; median must not crash to ~0."""
    hist = series(0, 0.01, 7, 7.02, 14)
    m = cycles.median_interval_days(hist)
    assert 6.5 <= m <= 7.5


def test_too_few_events_gives_none():
    """median_interval_days stays the ≥3-purchase number; a two-purchase
    history has a cycle, but only via estimate(), where its tier travels with it."""
    assert cycles.median_interval_days(series(0, 7)) is None
    # 3 raw events collapsing to 2 after coalescing is still too few
    assert cycles.median_interval_days(series(0, 0.1, 7)) is None
    assert cycles.estimate(series(0, 7)).trusted is False


def test_due_scoring_window():
    hist = series(0, 7, 14, 21)
    on_time = cycles.due_score(hist, T0 + timedelta(days=27))  # 6/7 ≈ 0.86 → due
    early = cycles.due_score(hist, T0 + timedelta(days=23))  # 2/7 ≈ 0.29 → not yet
    assert on_time >= cycles.DUE_MIN
    assert early < cycles.DUE_MIN


def test_suggest_orders_and_filters():
    now = T0 + timedelta(days=30)
    history = {
        1: series(0, 7, 14, 21),  # weekly, 9d since last → score ~1.3 → due
        2: series(0, 14, 28),  # biweekly, 2d since → 0.14 → not due
        3: series(0),  # one purchase → no cycle → never suggested
        4: series(0, 2, 4, 6),  # lapsed: 24d since a 2d-cycle → score 12 → retired
    }
    out = cycles.suggest(history, now)
    assert [s["catalog_id"] for s in out] == [1]
    assert out[0]["label"] == "weekly"
    assert out[0]["weeks"] == 1


def test_week_labels_have_no_upper_bin():
    """"Monthly" merged 3-week and 6-week items; weeks run open-ended instead."""
    assert cycles.cycle_label(3) == "more than weekly"
    assert cycles.cycle_label(8) == "weekly"
    assert cycles.cycle_label(15) == "bi-weekly"
    assert cycles.cycle_label(21) == "every 3 weeks"
    assert cycles.cycle_label(30) == "every 4 weeks"
    assert cycles.cycle_label(90) == "every 13 weeks"


def test_weeks_round_to_nearest_but_scoring_uses_measured_days():
    """A 17-day item is labelled bi-weekly yet judged at 17 days, not 14."""
    hist = series(0, 17, 34, 51)
    est = cycles.estimate(hist)
    assert est.weeks == 2
    assert est.cycle_days == 17
    # at 15 days elapsed it is due (15/17 = 0.88 >= 0.85) and trusted -> HIGH
    assert est.tier(T0 + timedelta(days=66)) == cycles.HIGH
    # at 14 days it is not due yet, but arrives inside the week -> POTENTIAL
    assert est.tier(T0 + timedelta(days=65)) == cycles.POTENTIAL


def test_trust_needs_both_enough_history_and_consistency():
    """`trusted` is evidence quality, not the tier. Two purchases, or three
    erratic ones, are untrusted — 4, 25, 9 days is not a rhythm."""
    assert cycles.estimate(series(0, 7, 14, 21)).trusted is True
    thin = cycles.estimate(series(0, 7))
    assert thin.trusted is False and thin.events == 2
    assert cycles.estimate(series(0, 4, 29, 38)).trusted is False
    once = cycles.estimate(series(0))
    assert once.trusted is False and once.cycle_days is None


def test_due_now_on_shaky_evidence_is_potential_not_high():
    """Same moment, same due-ness — only the evidence differs, and that is
    exactly what separates "buy it" from "you might need this"."""
    trusted = cycles.estimate(series(0, 20, 40, 60))
    thin = cycles.estimate(series(40, 60))  # same 20d cycle, same last buy, less proof
    now = T0 + timedelta(days=80)  # 20 days after each one's last buy → score 1.0
    assert trusted.tier(now) == cycles.HIGH
    assert thin.tier(now) == cycles.POTENTIAL


def test_untrusted_items_retire_sooner():
    """A shaky cycle stops nagging at 2x; a trusted one runs to 3x."""
    thin = cycles.estimate(series(0, 10))
    assert thin.tier(T0 + timedelta(days=28)) == cycles.POTENTIAL   # 1.8x
    assert thin.tier(T0 + timedelta(days=35)) is None               # 2.5x -> retired
    trusted = cycles.estimate(series(0, 10, 20, 30))
    assert trusted.tier(T0 + timedelta(days=55)) == cycles.HIGH     # 2.5x, still offered


def test_only_recent_intervals_count():
    """A rhythm that changed must stop arguing with the one in force now:
    four fresh weekly gaps outweigh an old monthly stretch entirely."""
    hist = series(0, 30, 60, 90, 97, 104, 111, 118)  # 30,30,30,7,7,7,7
    est = cycles.estimate(hist)
    assert est.cycle_days == 7          # the last 4 gaps only
    assert est.weeks == 1
    assert est.trusted is True          # and they are consistent


def test_no_cycle_item_is_never_suggested():
    now = T0 + timedelta(days=400)
    assert cycles.suggest({1: series(0)}, now) == []


def test_potential_reaches_a_week_ahead():
    """The second tier is a shopping horizon, not a confidence label: an item
    arriving inside the coming week is worth seeing before the trip, not after."""
    est = cycles.estimate(series(0, 20, 40, 60))  # 20d cycle, due at 17d elapsed
    assert est.tier(T0 + timedelta(days=68)) is None              # 8d in, due in 9d
    assert est.tier(T0 + timedelta(days=71)) == cycles.POTENTIAL  # 11d in, due in 6d
    assert est.tier(T0 + timedelta(days=77)) == cycles.HIGH       # due now


def test_a_week_spent_away_asks_for_almost_nothing():
    """The horizon is in-town days, so a week that is mostly a trip pulls
    almost nothing forward — you cannot eat groceries you are not home for."""
    from datetime import date as _date

    now = datetime(2026, 7, 30, 12, tzinfo=UTC)
    at_home = cycles.Away()
    # away for 6 of the next 7 days
    travelling = cycles.Away([_date(2026, 7, 31) + timedelta(days=n) for n in range(6)], UTC)
    assert cycles.week_horizon(now, at_home) == 7.0
    assert cycles.week_horizon(now, travelling) < 1.5


def test_high_requires_being_due_not_merely_trusted():
    """A rock-solid weekly item bought yesterday is not something to buy today."""
    est = cycles.estimate(series(0, 7, 14, 21))
    assert est.trusted is True
    # bought yesterday: due within 7 days, but only 1/7 through its cycle, so
    # the half-cycle floor keeps it off the list entirely
    assert est.tier(T0 + timedelta(days=22)) is None
    assert est.tier(T0 + timedelta(days=25)) == cycles.POTENTIAL  # 4/7 through
    assert est.tier(T0 + timedelta(days=27)) == cycles.HIGH       # due


def test_suggest_puts_buy_now_ahead_of_coming_up():
    now = T0 + timedelta(days=47)
    history = {
        1: series(20, 27, 34, 41),  # steady weekly, 6d since → due, trusted → HIGH
        2: series(0, 4, 29, 38),    # gaps 4,25,9 → erratic, 9d since → POTENTIAL
    }
    out = cycles.suggest(history, now)
    assert [s["tier"] for s in out] == [cycles.HIGH, cycles.POTENTIAL]
    assert [s["catalog_id"] for s in out] == [1, 2]
    assert all("due_in_days" in s for s in out)
