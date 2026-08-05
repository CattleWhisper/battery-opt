"""
Tests for the static seasonal baseline (core/static_schedule.py).

The static plan is both the comparison reference (the ~EUR 267/year
figure) and the production fallback: charge in vazio Nov-Apr, at
midday May-Oct, discharge in ponta — no price input at all. The
annual-saving check itself belongs to the backtest (Task 6); here we
verify structure and constraint compliance.
"""

import random
from datetime import date

import pytest

from custom_components.battery_opt.core.plan import (
    BatteryParams,
    soc_trajectory,
    validate_plan,
)
from custom_components.battery_opt.core.static_schedule import static_plan

PARAMS = BatteryParams()
N = 96
FLAT_LOAD = [1040.0] * N
NO_SOLAR = [0.0] * N


def _interval_hour(i: int) -> float:
    return i * 0.25


def test_winter_weekday_charges_overnight_discharges_in_ponta() -> None:
    """Thu 15 Jan 2026: charge only in vazio before 07:00, discharge in ponta."""
    plan = static_plan(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    for i in range(N):
        hour = _interval_hour(i)
        if plan.charge_w[i] > 0:
            assert hour < 7.0, f"charge outside vazio at {hour}"
        if plan.discharge_w[i] > 0:
            in_morning = 9.5 <= hour < 12.0
            in_evening = 18.5 <= hour < 21.0
            assert in_morning or in_evening, f"discharge outside ponta at {hour}"
    assert sum(plan.charge_w) > 0
    assert sum(plan.discharge_w) > 0


def test_summer_weekday_charges_at_midday() -> None:
    """
    Wed 15 Jul 2026: the charging window moves to 13:00-17:00.

    Starting from a full battery (the summer steady state, produced by
    the previous day's midday charge), the morning ponta is served.
    """
    params = BatteryParams(soc_start_kwh=5.0)
    plan = static_plan(date(2026, 7, 15), FLAT_LOAD, NO_SOLAR, params)
    for i in range(N):
        hour = _interval_hour(i)
        if plan.charge_w[i] > 0:
            assert 13.0 <= hour < 17.0, f"charge outside midday at {hour}"
        if plan.discharge_w[i] > 0:
            assert 9.25 <= hour < 12.25, f"discharge outside ponta at {hour}"
    assert sum(plan.charge_w) > 0
    assert sum(plan.discharge_w) > 0


def test_charging_window_switches_with_the_month() -> None:
    """January charges overnight; July charges at midday. Same weekday."""
    winter = static_plan(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    summer = static_plan(date(2026, 7, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    winter_hours = {_interval_hour(i) for i in range(N) if winter.charge_w[i] > 0}
    summer_hours = {_interval_hour(i) for i in range(N) if summer.charge_w[i] > 0}
    assert winter_hours
    assert max(winter_hours) < 7.0
    assert summer_hours
    assert min(summer_hours) >= 13.0


def test_weekend_days_do_nothing() -> None:
    """No ponta on Saturday/Sunday under the weekly cycle: no cycling."""
    for day in (date(2026, 1, 17), date(2026, 1, 18), date(2026, 7, 18)):
        plan = static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS)
        assert sum(plan.charge_w) == 0
        assert sum(plan.discharge_w) == 0


def test_takes_no_prices() -> None:
    """The signature itself: day, load, solar, params - nothing else."""
    plan = static_plan(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    assert len(plan) == N


def test_respects_constraints_over_random_days() -> None:
    """C-1..C-7 hold for random loads/solar across the year (mini property)."""
    rng = random.Random(42)
    for seed in range(50):
        rng.seed(seed)
        day = date(2026, 1, 1).fromordinal(
            date(2026, 1, 1).toordinal() + rng.randrange(365)
        )
        load = [rng.uniform(0.0, 3000.0) for _ in range(N)]
        solar = [rng.uniform(0.0, 800.0) for _ in range(N)]
        for start in (None, 5.0):
            params = BatteryParams(soc_start_kwh=start)
            plan = static_plan(day, load, solar, params)
            assert validate_plan(plan, load, solar, params) == [], f"{day} {start}"


def test_winter_discharge_stops_at_reserve_floor() -> None:
    """
    Winter ponta demand exceeds the stored energy: dry at the floor.

    5 h of ponta at 1.04 kW wants 5.2 kWh; only 3.65 kWh is stored.
    The battery must stop at the reserve floor, never below it.
    """
    plan = static_plan(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    soc = soc_trajectory(plan, PARAMS)
    assert min(soc) >= PARAMS.cap_min_kwh - 1e-9
    # It genuinely runs out: total meter discharge equals the usable
    # stored energy, not the full 5.2 kWh the winter ponta could absorb.
    discharged_kwh = sum(plan.discharge_w) * 0.25 / 1000
    stored_kwh = (PARAMS.cap_usable_kwh - PARAMS.cap_min_kwh) * PARAMS.eta_one_way
    assert discharged_kwh == pytest.approx(stored_kwh, rel=1e-6)
