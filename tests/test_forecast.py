"""
Tests for core.forecast (plan Task 11).

Pure-function tests only — no Home Assistant, no recorder. The HA
adapter (load_history.py) is covered separately by monkeypatching it
away in coordinator-level tests; this file exercises forecast_load()
exhaustively per the overnight-session decision.
"""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.battery_opt.core.forecast import (
    BASE_LOAD_W,
    INTERVALS_PER_DAY,
    DaySample,
    forecast_load,
)

N = INTERVALS_PER_DAY
ZERO_SOLAR = [0.0] * N


def _flat_sample(day: date, value: float, n: int = N) -> DaySample:
    return DaySample(day=day, load_w=(value,) * n)


def test_fewer_than_four_same_weekday_days_is_entirely_flat() -> None:
    """Fewer than 4 weeks of same-weekday history -> flat base load."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        _flat_sample(date(2026, 7, 29), 2000.0),  # Wed
        _flat_sample(date(2026, 7, 22), 2000.0),  # Wed
        _flat_sample(date(2026, 7, 15), 2000.0),  # Wed
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result == [BASE_LOAD_W] * N


def test_zero_same_weekday_history_is_flat() -> None:
    """No history at all -> flat base load, not an error."""
    target = date(2026, 8, 5)
    result = forecast_load(target, [], ZERO_SOLAR)
    assert result == [BASE_LOAD_W] * N


def test_four_same_weekday_days_use_the_median() -> None:
    """With exactly 4 occurrences, each slot is their median."""
    target = date(2026, 8, 5)  # Wednesday
    # Four Wednesdays, one week apart, each flat across the whole day.
    samples = [
        _flat_sample(date(2026, 7, 8), 1000.0),
        _flat_sample(date(2026, 7, 15), 2000.0),
        _flat_sample(date(2026, 7, 22), 3000.0),
        _flat_sample(date(2026, 7, 29), 4000.0),
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result == [2500.0] * N  # median of 1000/2000/3000/4000


def test_only_the_most_recent_occurrences_count() -> None:
    """A 5th, older same-weekday day is ignored, even as an outlier."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        _flat_sample(date(2026, 7, 1), 999_999.0),  # far older Wednesday: ignored
        _flat_sample(date(2026, 7, 8), 1000.0),
        _flat_sample(date(2026, 7, 15), 2000.0),
        _flat_sample(date(2026, 7, 22), 3000.0),
        _flat_sample(date(2026, 7, 29), 4000.0),
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result == [2500.0] * N  # median of 1000/2000/3000/4000


def test_other_weekdays_and_future_days_are_excluded() -> None:
    """Same-day-of-month Thursdays and a same-weekday future day don't count."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        _flat_sample(date(2026, 7, 30), 1000.0),  # Thursday: wrong weekday
        _flat_sample(date(2026, 8, 12), 1000.0),  # next Wednesday: in the future
        _flat_sample(date(2026, 7, 8), 1000.0),
        _flat_sample(date(2026, 7, 15), 2000.0),
        _flat_sample(date(2026, 7, 22), 3000.0),
    ]
    # Only 3 valid same-weekday, past days -> flat base load.
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result == [BASE_LOAD_W] * N


def test_slot_with_a_missing_value_falls_back_individually() -> None:
    """One same-weekday day missing a slot: only that slot falls back."""
    target = date(2026, 8, 5)  # Wednesday
    full = [1000.0] * N
    with_gap = [1000.0] * N
    with_gap[10] = None  # one missing quarter-hour
    samples = [
        DaySample(day=date(2026, 7, 8), load_w=tuple(full)),
        DaySample(day=date(2026, 7, 15), load_w=tuple(full)),
        DaySample(day=date(2026, 7, 22), load_w=tuple(full)),
        DaySample(day=date(2026, 7, 29), load_w=tuple(with_gap)),
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result[10] == BASE_LOAD_W  # only 3 values available at slot 10
    assert result[0] == 1000.0  # every other slot has all 4 values


def test_solar_is_subtracted_and_floored_at_zero() -> None:
    """Net load = max(0, forecast - solar), per spec §6."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        _flat_sample(date(2026, 7, 8), 1000.0),
        _flat_sample(date(2026, 7, 15), 1000.0),
        _flat_sample(date(2026, 7, 22), 1000.0),
        _flat_sample(date(2026, 7, 29), 1000.0),
    ]
    solar = [0.0] * N
    solar[40] = 400.0  # partial self-consumption
    solar[41] = 5000.0  # solar exceeds load: net load floors at 0
    result = forecast_load(target, samples, solar)
    assert result[0] == 1000.0
    assert result[40] == 600.0
    assert result[41] == 0.0


def test_mismatched_solar_length_raises() -> None:
    """solar_w must match n_intervals; a silent truncation would be worse."""
    with pytest.raises(ValueError, match="solar_w"):
        forecast_load(date(2026, 8, 5), [], [0.0] * (N - 1))


def test_flat_fallback_also_nets_solar() -> None:
    """Even the <4-weeks flat fallback still nets solar (not a shortcut path)."""
    target = date(2026, 8, 5)
    solar = [0.0] * N
    solar[0] = 200.0
    result = forecast_load(target, [], solar)
    assert result[0] == pytest.approx(BASE_LOAD_W - 200.0)
    assert result[1] == BASE_LOAD_W
