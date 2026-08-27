"""
Tests for core.appliance: maximal cheap appliance-window detection.

Pure-core tests (no HA): the service-level behaviour of
`battery_opt.get_best_periods` is covered in test_services.py.
Windows are maximal cheap runs (owner 2026-08-17): every contiguous
stretch at or below min + threshold x range is reported whole, in
time order.
"""

from __future__ import annotations

import pytest

from custom_components.battery_opt.core.appliance import (
    ApplianceWindow,
    cheap_windows,
    expensive_windows,
    price_cutoff,
)


def test_reports_the_whole_cheap_valley() -> None:
    """A long valley comes out as ONE maximal window, never a clip."""
    prices = [0.25] * 96
    prices[49:64] = [0.185] * 15  # 12:15-16:00
    windows = cheap_windows(prices, threshold_fraction=0.2, min_quarters=2, count=3)
    assert [(w.start_index, w.end_index) for w in windows] == [(49, 64)]
    assert windows[0].avg_price_eur_kwh == pytest.approx(0.185)


def test_short_dip_and_long_valley_both_surface_in_time_order() -> None:
    """
    The owner's 2026-08-17 example day: dip 08:45-09:15, valley 12:15-16:00.

    Cutoff = 0.19 + 0.2 x (0.397 - 0.19) ~ 0.231: the 0.21 dip and the
    0.19 valley qualify, the ~0.26 night does not — and the result is
    chronological even though the valley is the cheaper of the two.
    """
    prices = [0.26] * 96
    prices[35:37] = [0.21] * 2  # 08:45-09:15 dip before the spike
    prices[37:49] = [0.397] * 12  # 09:15-12:15 ponta
    prices[49:64] = [0.19] * 15  # 12:15-16:00 valley
    windows = cheap_windows(prices, threshold_fraction=0.2, min_quarters=2, count=3)
    assert [(w.start_index, w.end_index) for w in windows] == [(35, 37), (49, 64)]
    assert windows[0].avg_price_eur_kwh > windows[1].avg_price_eur_kwh


def test_expensive_windows_mirror_the_top_of_the_range() -> None:
    """
    The traffic-light red tier: maximal runs near the day's maximum.

    Same example day: cutoff = 0.397 - 0.2 x (0.397 - 0.19) ~ 0.356,
    so only the ponta block qualifies — whole, in time order, with its
    true (positive) average.
    """
    prices = [0.26] * 96
    prices[35:37] = [0.21] * 2
    prices[37:49] = [0.397] * 12  # 09:15-12:15 ponta
    prices[49:64] = [0.19] * 15
    windows = expensive_windows(prices, threshold_fraction=0.2, min_quarters=2, count=3)
    assert [(w.start_index, w.end_index) for w in windows] == [(37, 49)]
    assert windows[0].avg_price_eur_kwh == pytest.approx(0.397)


def test_min_quarters_drops_too_short_runs() -> None:
    """A 30-min dip disappears when the caller needs at least 45 min."""
    prices = [0.26] * 96
    prices[35:37] = [0.21] * 2
    prices[49:64] = [0.19] * 15
    windows = cheap_windows(prices, threshold_fraction=0.2, min_quarters=3, count=3)
    assert [(w.start_index, w.end_index) for w in windows] == [(49, 64)]


def test_count_keeps_the_cheapest_runs_in_time_order() -> None:
    """Over the cap: the most expensive run drops, order stays temporal."""
    prices = [0.30] * 96
    prices[8:12] = [0.05] * 4
    prices[40:44] = [0.02] * 4
    prices[80:84] = [0.06] * 4
    windows = cheap_windows(prices, threshold_fraction=0.2, min_quarters=2, count=2)
    assert [(w.start_index, w.end_index) for w in windows] == [(8, 12), (40, 44)]


def test_cutoff_is_computed_within_the_bounds() -> None:
    """An excluded night minimum must not starve the reachable range."""
    prices = [0.20] * 96
    prices[0:4] = [0.01] * 4  # night: far cheaper, but out of bounds
    prices[52:60] = [0.15] * 8
    windows = cheap_windows(
        prices, threshold_fraction=0.2, min_quarters=2, count=3, first_quarter=32
    )
    assert [(w.start_index, w.end_index) for w in windows] == [(52, 60)]
    cutoff = price_cutoff(prices, 0.2, first_quarter=32)
    assert cutoff == pytest.approx(0.15 + 0.2 * 0.05)


def test_runs_clip_at_the_bounds() -> None:
    """A run crossing `after` starts at the bound, not before it."""
    prices = [0.30] * 96
    prices[30:40] = [0.10] * 10
    windows = cheap_windows(
        prices, threshold_fraction=0.2, min_quarters=2, count=3, first_quarter=32
    )
    assert [(w.start_index, w.end_index) for w in windows] == [(32, 40)]


def test_negative_prices_are_handled() -> None:
    """Nothing assumes prices are positive (project rule)."""
    prices = [0.10] * 96
    prices[60:66] = [-0.25] * 6
    windows = cheap_windows(prices, threshold_fraction=0.2, min_quarters=2, count=3)
    assert [(w.start_index, w.end_index) for w in windows] == [(60, 66)]
    assert windows[0].avg_price_eur_kwh == pytest.approx(-0.25)


def test_flat_day_is_one_all_day_window() -> None:
    """Zero spread: everything is cheap, honestly — any time is fine."""
    windows = cheap_windows(
        [0.10] * 96, threshold_fraction=0.2, min_quarters=2, count=3
    )
    assert [(w.start_index, w.end_index) for w in windows] == [(0, 96)]


def test_flat_day_has_no_expensive_tier() -> None:
    """The mirror's deliberate asymmetry: nothing to avoid on a flat day."""
    assert (
        expensive_windows([0.10] * 96, threshold_fraction=0.2, min_quarters=2, count=3)
        == []
    )


def test_empty_range_yields_nothing() -> None:
    """Empty or inverted bounds produce no windows and no cutoff."""
    assert cheap_windows([], 0.2, 2, 3) == []
    assert cheap_windows([0.1] * 96, 0.2, 2, 3, first_quarter=90, last_quarter=90) == []
    assert price_cutoff([0.1] * 96, 0.2, first_quarter=96) is None


def test_reference_widens_a_flat_days_narrow_range() -> None:
    """
    The motivating case: a flat day judged against a spread-out week.

    Alone, this day's tiny 0.10-0.12 wobble would call its own top
    third "expensive". Against a week that actually swung 0.02-0.40,
    the whole day sits in the bottom of a much wider range and reads
    as uniformly cheap instead.
    """
    today = [0.10] * 96
    today[40:44] = [0.12] * 4  # a small wobble, entirely self-contained
    history = [0.40, 0.02] * 48  # last week's real spread
    # Cutoff = 0.02 + 0.3 x (0.40 - 0.02) = 0.134: both 0.10 and 0.12
    # clear it, so the whole day comes out as one cheap window.
    windows = cheap_windows(today, 0.3, 2, 3, reference_prices_eur_kwh=history)
    assert windows == [
        ApplianceWindow(
            start_index=0,
            end_index=96,
            avg_price_eur_kwh=pytest.approx(sum(today) / 96),
        ),
    ]
    # And nothing crosses the expensive cutoff = 0.40 - 0.3x0.38 ~ 0.286.
    assert expensive_windows(today, 0.3, 2, 3, reference_prices_eur_kwh=history) == []


def test_reference_falls_back_to_the_days_own_range_when_empty() -> None:
    """No reference (or an empty one) reproduces the single-day cutoff exactly."""
    prices = [0.26] * 96
    prices[35:37] = [0.21] * 2
    prices[37:49] = [0.397] * 12
    prices[49:64] = [0.19] * 15
    plain = cheap_windows(prices, 0.2, 2, 3)
    assert cheap_windows(prices, 0.2, 2, 3, reference_prices_eur_kwh=[]) == plain
    assert cheap_windows(prices, 0.2, 2, 3, reference_prices_eur_kwh=None) == plain
    assert price_cutoff(prices, 0.2, reference_prices_eur_kwh=[]) == price_cutoff(
        prices, 0.2
    )


def test_reference_can_narrow_a_days_range_and_grow_the_expensive_tier() -> None:
    """A day judged against a narrower reference gets a stricter cutoff."""
    prices = [0.20] * 96
    prices[10:18] = [0.30] * 8  # today's own mid shoulder
    prices[40:44] = [0.50] * 4  # today's own spike
    # Self-comparison: cutoff = 0.50 - 0.2 x (0.50-0.20) = 0.44 — only
    # the spike clears it.
    assert [
        (w.start_index, w.end_index) for w in expensive_windows(prices, 0.2, 2, 3)
    ] == [(40, 44)]
    # Against a week that barely moved (0.20-0.22), the same 20%
    # fraction gives cutoff = 0.22 - 0.2 x 0.02 ~ 0.216: the shoulder
    # ALSO clears it now, even though its own value never changed.
    narrow_reference = [0.20, 0.22] * 48
    windows = expensive_windows(
        prices, 0.2, 2, 3, reference_prices_eur_kwh=narrow_reference
    )
    assert [(w.start_index, w.end_index) for w in windows] == [(10, 18), (40, 44)]


def test_reference_only_moves_the_range_membership_stays_the_days_own() -> None:
    """Windows are still built from the DAY's own values, never the reference's."""
    prices = [0.10] * 96
    prices[20:24] = [0.50] * 4
    reference = [-100.0, 100.0]  # a wildly wide, unrelated reference
    windows = cheap_windows(prices, 0.5, 2, 3, reference_prices_eur_kwh=reference)
    # Cutoff = -100 + 0.5 x 200 = 0: nothing in `prices` (all >= 0.10)
    # can be at or below it.
    assert windows == []
