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
    apply_intraday_correction,
    forecast_load,
    merge_day_samples,
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


def test_slot_missing_from_one_of_four_days_still_medians() -> None:
    """
    A gap in ONE of the 4 days medians the remaining 3, not a flat fallback.

    Owner 2026-08-27 (forecast-accuracy hardening): the old rule
    required all 4 values present or the slot dropped to BASE_LOAD_W —
    a single recorder gap in one historical day quietly flattened that
    slot every day thereafter. Default `min_slot_samples` (2) means 3
    of 4 present is enough to still take a real median.
    """
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        DaySample(day=date(2026, 7, 8), load_w=tuple([1000.0] * N)),
        DaySample(day=date(2026, 7, 15), load_w=tuple([2000.0] * N)),
        DaySample(day=date(2026, 7, 22), load_w=tuple([3000.0] * N)),
        DaySample(
            day=date(2026, 7, 29),
            load_w=tuple(4000.0 if i != 10 else None for i in range(N)),
        ),
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result[10] == 2000.0  # median of the 3 present: 1000/2000/3000
    assert result[0] == 2500.0  # every other slot still has all 4


def test_slot_missing_from_three_of_four_days_falls_back() -> None:
    """Below `min_slot_samples` (2), a slot still falls back to base load."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        DaySample(day=date(2026, 7, 8), load_w=tuple([1000.0] * N)),
        DaySample(
            day=date(2026, 7, 15),
            load_w=tuple(None if i == 10 else 1000.0 for i in range(N)),
        ),
        DaySample(
            day=date(2026, 7, 22),
            load_w=tuple(None if i == 10 else 1000.0 for i in range(N)),
        ),
        DaySample(
            day=date(2026, 7, 29),
            load_w=tuple(None if i == 10 else 1000.0 for i in range(N)),
        ),
    ]
    result = forecast_load(target, samples, ZERO_SOLAR)
    assert result[10] == BASE_LOAD_W  # only 1 value present, below the minimum
    assert result[0] == 1000.0  # every other slot has all 4


def test_min_slot_samples_is_tunable() -> None:
    """`min_slot_samples` overrides the default, per slot's own contract."""
    target = date(2026, 8, 5)  # Wednesday
    samples = [
        DaySample(day=date(2026, 7, 8), load_w=tuple([1000.0] * N)),
        DaySample(
            day=date(2026, 7, 15),
            load_w=tuple(None if i == 10 else 2000.0 for i in range(N)),
        ),
        DaySample(
            day=date(2026, 7, 22),
            load_w=tuple(None if i == 10 else 3000.0 for i in range(N)),
        ),
        DaySample(
            day=date(2026, 7, 29),
            load_w=tuple(None if i == 10 else 4000.0 for i in range(N)),
        ),
    ]
    # Restoring the old strict all-4 requirement: the single present
    # value at slot 10 (1000.0) is not enough on its own.
    strict = forecast_load(target, samples, ZERO_SOLAR, min_slot_samples=4)
    assert strict[10] == BASE_LOAD_W
    # A minimum of 1 accepts even a lone value as its own "median".
    lenient = forecast_load(target, samples, ZERO_SOLAR, min_slot_samples=1)
    assert lenient[10] == 1000.0


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


def test_merge_prefers_primary_on_a_shared_date() -> None:
    """Stage 2: the archive (primary) wins over the adapter on a shared date."""
    shared = date(2026, 7, 29)
    primary = [_flat_sample(shared, 1234.0)]
    fallback = [_flat_sample(shared, 9999.0)]
    merged = merge_day_samples(primary, fallback)
    assert len(merged) == 1
    assert merged[0].day == shared
    assert merged[0].load_w[0] == 1234.0


def test_merge_keeps_fallback_only_dates() -> None:
    """A date the archive hasn't reached yet still comes from the adapter."""
    archived_day = date(2026, 7, 29)
    adapter_only_day = date(2026, 7, 22)
    primary = [_flat_sample(archived_day, 1234.0)]
    fallback = [
        _flat_sample(archived_day, 9999.0),
        _flat_sample(adapter_only_day, 5678.0),
    ]
    merged = merge_day_samples(primary, fallback)
    by_day = {s.day: s for s in merged}
    assert set(by_day) == {archived_day, adapter_only_day}
    assert by_day[archived_day].load_w[0] == 1234.0  # primary still wins
    assert by_day[adapter_only_day].load_w[0] == 5678.0  # only in fallback


def test_merge_of_two_empty_sources_is_empty() -> None:
    """No archive, no adapter data: merge degrades to an empty list."""
    assert merge_day_samples([], []) == []


def test_forecast_load_uses_merged_samples_directly() -> None:
    """merge_day_samples' output feeds forecast_load unchanged."""
    target = date(2026, 8, 5)  # Wednesday
    archived = [
        _flat_sample(date(2026, 7, 8), 1000.0),
        _flat_sample(date(2026, 7, 22), 3000.0),
    ]
    adapter = [
        _flat_sample(date(2026, 7, 8), 9999.0),  # shadowed by the archive
        _flat_sample(date(2026, 7, 15), 2000.0),
        _flat_sample(date(2026, 7, 29), 4000.0),
    ]
    merged = merge_day_samples(archived, adapter)
    result = forecast_load(target, merged, ZERO_SOLAR)
    assert result == [2500.0] * N  # median of 1000/2000/3000/4000, archive-preferred


def _observed(elapsed_quarters: int, value: float | None) -> list[float | None]:
    return [value] * elapsed_quarters + [None] * (N - elapsed_quarters)


def test_intraday_correction_scales_only_the_remaining_quarters() -> None:
    """Elapsed quarters (default 4) run 2x forecast -> the REST scales 2x."""
    forecast = [1000.0] * N
    observed = _observed(4, 2000.0)
    result = apply_intraday_correction(forecast, observed)
    assert result[:4] == [1000.0] * 4  # elapsed stays the ORIGINAL forecast
    assert result[4:] == pytest.approx([2000.0] * (N - 4))


def test_intraday_correction_below_minimum_is_a_noop() -> None:
    """Fewer than min_elapsed_quarters (default 4) of data -> unchanged."""
    forecast = [1000.0] * N
    observed = _observed(3, 2000.0)
    assert apply_intraday_correction(forecast, observed) == forecast


def test_intraday_correction_clamps_a_high_ratio() -> None:
    """A 10x morning does not scale the rest of the day by 10x."""
    forecast = [1000.0] * N
    observed = _observed(4, 10_000.0)  # raw ratio 10.0
    result = apply_intraday_correction(forecast, observed)
    assert result[4:] == pytest.approx([3000.0] * (N - 4))  # MAX_CORRECTION_RATIO


def test_intraday_correction_clamps_a_low_ratio() -> None:
    """A near-zero morning does not zero out the rest of the day."""
    forecast = [1000.0] * N
    observed = _observed(4, 10.0)  # raw ratio 0.01
    result = apply_intraday_correction(forecast, observed)
    assert result[4:] == pytest.approx([300.0] * (N - 4))  # MIN_CORRECTION_RATIO


def test_intraday_correction_skips_when_forecast_so_far_is_near_zero() -> None:
    """A zero-forecast morning (e.g. solar fully covering it) blocks the ratio."""
    forecast = [0.0] * 4 + [1000.0] * (N - 4)
    observed = _observed(4, 500.0)
    result = apply_intraday_correction(forecast, observed)
    assert result == forecast  # unchanged: no safe ratio to compute


def test_intraday_correction_length_mismatch_raises() -> None:
    """observed_w must match forecast_w, like forecast_load's own solar_w check."""
    with pytest.raises(ValueError, match="observed_w"):
        apply_intraday_correction([1000.0] * N, [500.0] * (N - 1))


def test_intraday_correction_tunables_override() -> None:
    """Every keyword is independently overridable, per the module's contract."""
    forecast = [1000.0] * N
    observed = _observed(1, 4000.0)  # raw ratio 4.0, only 1 elapsed quarter
    result = apply_intraday_correction(
        forecast,
        observed,
        min_elapsed_quarters=1,
        min_ratio=0.5,
        max_ratio=2.0,
    )
    assert result[1:] == pytest.approx([2000.0] * (N - 1))  # clamped to max_ratio=2.0
