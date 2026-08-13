"""
Tests for the static seasonal baseline (core/static_schedule.py).

The static plan is both the comparison reference (the ~EUR 267/year
figure) and the production fallback: charge in vazio Nov-Apr, at
midday May-Oct, discharge in ponta — no price input at all. The
annual-saving check itself belongs to the backtest (Task 6); here we
verify structure and constraint compliance.
"""

import dataclasses
import random
from datetime import date, datetime, timedelta

import pytest

from custom_components.battery_opt.core.calendar import period
from custom_components.battery_opt.core.plan import (
    BatteryParams,
    soc_trajectory,
    validate_plan,
)
from custom_components.battery_opt.core.static_schedule import (
    chained_start_soc,
    static_plan,
)

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


def test_charge_window_stays_armed_after_model_full() -> None:
    """
    Owner decision 2026-08-13: the CHARGE state spans the WHOLE window.

    The model fills to capacity in the first ~2 h; the remaining
    window quarters carry a marginal state-selector power (ADR-0007)
    shaved off the filled ones — totals, the end SoC and therefore the
    day-chaining seed are conserved. At run time the charge loop and
    the firmware full-target own the stop, so real shortfalls recover
    in the armed time.
    """
    winter = static_plan(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    # Every quarter of the 00:00-07:00 window is a charge quarter...
    assert all(winter.charge_w[i] > 0 for i in range(28))
    # ...but the armed tail is marginal, not an energy claim.
    assert winter.charge_w[27] < 1.0
    # Energy conserved: the trajectory still ends exactly at the floor.
    assert soc_trajectory(winter, PARAMS)[-1] == pytest.approx(PARAMS.cap_min_kwh)
    assert validate_plan(winter, FLAT_LOAD, NO_SOLAR, PARAMS) == []

    seed = chained_start_soc(date(2026, 7, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    params = dataclasses.replace(PARAMS, soc_start_kwh=seed)
    summer = static_plan(date(2026, 7, 15), FLAT_LOAD, NO_SOLAR, params)
    assert all(summer.charge_w[i] > 0 for i in range(52, 68))  # 13:00-17:00
    assert summer.charge_w[67] < 1.0
    assert validate_plan(summer, FLAT_LOAD, NO_SOLAR, params) == []


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


def test_chained_start_soc_summer_carries_charge_over_the_weekend() -> None:
    """Fri charges to full, Sat/Sun are no-ops -> Monday starts full."""
    start = chained_start_soc(date(2026, 8, 10), FLAT_LOAD, NO_SOLAR, PARAMS)
    assert start == pytest.approx(PARAMS.cap_usable_kwh)


def test_chained_start_soc_winter_is_the_reserve_floor() -> None:
    """
    Winter weekdays end drained, so chaining changes nothing there.

    Ponta demand exceeds the one-unit deliverable (5.2 vs 3.46 kWh —
    the Checkpoint B finding): every winter weekday ends at the floor,
    making the chained seed exactly the old floor seed.
    """
    start = chained_start_soc(date(2026, 1, 15), FLAT_LOAD, NO_SOLAR, PARAMS)
    assert start == pytest.approx(PARAMS.cap_min_kwh)


def test_chained_summer_day_discharges_its_morning_ponta() -> None:
    """
    Seeded from the previous weekday's end, morning ponta discharges.

    The production gap this fixes: a floor-seeded summer day cannot
    discharge (ponta 09:15-12:15 precedes the 13:00-17:00 charge), so
    without chaining the battery would sit full all summer — and only
    ponta may discharge under the chained seed.
    """
    day = date(2026, 8, 10)  # Monday
    floor_seeded = static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS)
    assert sum(floor_seeded.discharge_w) == 0  # the bug, demonstrated

    start = chained_start_soc(day, FLAT_LOAD, NO_SOLAR, PARAMS)
    chained = static_plan(
        day,
        FLAT_LOAD,
        NO_SOLAR,
        dataclasses.replace(PARAMS, soc_start_kwh=start),
    )
    assert sum(chained.discharge_w) > 0
    for i, discharge in enumerate(chained.discharge_w):
        if discharge > 0:
            when = datetime(2026, 8, 10) + timedelta(minutes=15 * i)
            assert period(when) == "ponta"
    # Still a valid plan under the seeded start.
    seeded = dataclasses.replace(PARAMS, soc_start_kwh=start)
    assert validate_plan(chained, FLAT_LOAD, NO_SOLAR, seeded) == []
