"""Interval-estimator tests with synthetic histories (PLAN.md Phase 1 gate)."""

import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import cycles  # noqa: E402

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def series(*day_offsets):
    return [(T0 + timedelta(days=d)).isoformat() for d in day_offsets]


def test_weekly_item():
    hist = series(0, 7, 14, 22, 28)  # weekly with jitter
    m = cycles.median_interval_days(hist)
    assert 6.5 <= m <= 7.5
    assert cycles.cycle_label(m) == "weekly"


def test_biweekly_with_one_skip():
    """One vacation skip (a 28d gap) must not drag a biweekly item to monthly."""
    hist = series(0, 14, 28, 56, 70)  # 14,14,28,14 → median 14
    m = cycles.median_interval_days(hist)
    assert m == 14
    assert cycles.cycle_label(m) == "bi-weekly"


def test_burst_coalescing():
    """Two checkoffs the same evening = one purchase; median must not crash to ~0."""
    hist = series(0, 0.01, 7, 7.02, 14)
    m = cycles.median_interval_days(hist)
    assert 6.5 <= m <= 7.5


def test_too_few_events_gives_none():
    assert cycles.median_interval_days(series(0, 7)) is None
    # 3 raw events collapsing to 2 after coalescing is still too few
    assert cycles.median_interval_days(series(0, 0.1, 7)) is None


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
        3: series(0, 7),  # too few events → never suggested
        4: series(0, 2, 4, 6),  # lapsed: 24d since a 2d-cycle → score 12 → retired
    }
    out = cycles.suggest(history, now)
    assert [s["catalog_id"] for s in out] == [1]
    assert out[0]["label"] == "weekly"


def test_bin_labels():
    assert cycles.cycle_label(3) == "twice a week"
    assert cycles.cycle_label(8) == "weekly"
    assert cycles.cycle_label(15) == "bi-weekly"
    assert cycles.cycle_label(30) == "monthly"
    assert cycles.cycle_label(90) == "occasional"
