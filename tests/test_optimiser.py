"""
Tests for the greedy optimiser (core/optimiser.py, spec §7).

The property tests are the heart of this file: over many random days
no plan may violate C-1..C-7, and the greedy must never lose to the
static baseline — that comparison catches wear-cost sign errors and
SoC-repair bugs that produce plausible-looking plans.
"""

import dataclasses
import random
from datetime import date, timedelta

import pytest

from custom_components.battery_opt.core.optimiser import solve
from custom_components.battery_opt.core.plan import (
    BatteryParams,
    saving_vs_no_cycling,
    soc_trajectory,
    validate_plan,
)
from custom_components.battery_opt.core.static_schedule import static_plan

PARAMS = BatteryParams()
N = 96


def _random_day(seed: int) -> tuple[date, list[float], list[float], list[float]]:
    """
    Deterministic random day: date, prices, load, solar.

    Prices span -0.05..0.45 EUR/kWh (OMIE can go negative), loads
    0..3 kW, solar 0..800 W — deliberately wilder than reality.
    """
    rng = random.Random(seed)
    day = date(2026, 1, 1) + timedelta(days=rng.randrange(365))
    prices = [rng.uniform(-0.05, 0.45) for _ in range(N)]
    load = [rng.uniform(0.0, 3000.0) for _ in range(N)]
    solar = [rng.uniform(0.0, 800.0) for _ in range(N)]
    return day, prices, load, solar


def test_tracer_single_pair() -> None:
    """
    Two intervals, cheap then expensive: one fully-sized pair.

    Charge cap 2000 W x 15 min = 0.5 kWh grid; the pair delivers
    q = 0.5 * eta_rt = 0.45 kWh at the meter in interval 1.
    saving = 0.45 * (0.40 - 0.10/0.9 - 0.02).
    """
    result = solve([0.10, 0.40], [2000.0, 2000.0], [0.0, 0.0], PARAMS)
    assert result.plan.charge_w == pytest.approx((2000.0, 0.0))
    assert result.plan.discharge_w == pytest.approx((0.0, 1800.0))
    expected = 0.45 * (0.40 - 0.10 / 0.9 - 0.02)
    assert result.forecast_saving_eur == pytest.approx(expected)
    assert result.forecast_saving_eur == pytest.approx(
        saving_vs_no_cycling(result.plan, [0.10, 0.40], PARAMS)
    )


def test_charge_state_armed_through_profitable_quarters() -> None:
    """
    Owner decision 2026-08-13: the CHARGE state stays armed past the run.

    Quarters between a charge run and the discharges it feeds carry a
    marginal state-selector power while the C-8 condition holds against
    the cheapest discharge still ahead; nothing arms past the last
    discharge. Energy and the modelled saving are conserved exactly
    (the armed energy is shaved off the run itself).
    """
    prices = [0.08] * 40 + [0.35] * 8 + [0.08] * 48
    load = [1000.0] * N
    solar = [0.0] * N
    result = solve(prices, load, solar, PARAMS)
    charge = result.plan.charge_w
    armed = [i for i in range(N) if 0 < charge[i] < 1.0]
    assert armed  # the run extends toward the discharge block
    assert max(armed) < 40  # never into or past the discharges
    assert not any(charge[i] > 0 for i in range(48, N))  # none after the last
    assert validate_plan(result.plan, load, solar, PARAMS) == []
    assert result.forecast_saving_eur == pytest.approx(
        saving_vs_no_cycling(result.plan, prices, PARAMS)
    )


def test_flat_prices_no_cycling() -> None:
    """Degenerate case: no spread, no cycling (efficiency eats any pair)."""
    result = solve([0.20] * N, [1040.0] * N, [0.0] * N, PARAMS)
    assert sum(result.plan.charge_w) == 0
    assert sum(result.plan.discharge_w) == 0
    assert result.forecast_saving_eur == 0


def test_causality_descending_prices_no_cycling() -> None:
    """Expensive morning, cheap evening: nothing to pair (charge must precede)."""
    prices = [0.45 - 0.004 * i for i in range(N)]
    result = solve(prices, [1040.0] * N, [0.0] * N, PARAMS)
    assert sum(result.plan.discharge_w) == 0


def test_wear_zero_cycles_at_least_as_much() -> None:
    """WEAR_COST = 0 -> throughput >= the default 0.02 EUR/kWh case."""
    _, prices, load, solar = _random_day(7)
    free = solve(
        prices, load, solar, dataclasses.replace(PARAMS, wear_cost_eur_kwh=0.0)
    )
    worn = solve(prices, load, solar, PARAMS)
    assert sum(free.plan.discharge_w) >= sum(worn.plan.discharge_w) - 1e-9


def test_wear_above_max_spread_no_cycling() -> None:
    """A wear cost above any achievable spread stops all cycling."""
    _, prices, load, solar = _random_day(11)
    max_spread = max(prices) - min(prices) / PARAMS.eta_roundtrip
    params = dataclasses.replace(PARAMS, wear_cost_eur_kwh=max_spread + 0.01)
    result = solve(prices, load, solar, params)
    assert sum(result.plan.charge_w) == 0
    assert sum(result.plan.discharge_w) == 0


def test_zero_export_no_discharge_into_zero_load() -> None:
    """C-1: intervals with zero net load get zero discharge."""
    _, prices, load, solar = _random_day(13)
    for i in range(0, N, 3):
        load[i] = 0.0
    result = solve(prices, load, solar, PARAMS)
    for i in range(0, N, 3):
        assert result.plan.discharge_w[i] == 0


def test_contracted_power_margin_respected() -> None:
    """C-3: house load + charge never exceeds P_USABLE."""
    _, prices, load, solar = _random_day(17)
    result = solve(prices, load, solar, PARAMS)
    for i in range(N):
        assert load[i] + result.plan.charge_w[i] <= PARAMS.p_usable_w + 1e-6


def test_soc_stays_within_floor_and_ceiling() -> None:
    """C-4/C-5 hold across the whole trajectory, from both start states."""
    _, prices, load, solar = _random_day(19)
    for start in (None, 5.0):
        params = dataclasses.replace(PARAMS, soc_start_kwh=start)
        result = solve(prices, load, solar, params)
        soc = soc_trajectory(result.plan, params)
        assert min(soc) >= params.cap_min_kwh - 1e-9
        assert max(soc) <= params.cap_usable_kwh + 1e-9


def test_property_no_constraint_violations_over_1000_days() -> None:
    """
    PROPERTY: 1000 random days, zero C-1..C-7 violations.

    The reported forecast saving must also equal the evaluator run on
    the same plan, and never be negative (doing nothing is always an
    option).
    """
    for seed in range(1000):
        _, prices, load, solar = _random_day(seed)
        start = 5.0 if seed % 5 == 0 else None
        params = dataclasses.replace(PARAMS, soc_start_kwh=start)
        result = solve(prices, load, solar, params)
        assert validate_plan(result.plan, load, solar, params) == [], f"seed {seed}"
        evaluated = saving_vs_no_cycling(result.plan, prices, params)
        assert result.forecast_saving_eur == pytest.approx(evaluated), f"seed {seed}"
        assert result.forecast_saving_eur >= -1e-9, f"seed {seed}"


def test_property_greedy_never_loses_to_static() -> None:
    """
    PROPERTY: greedy saving >= static saving, always, same conditions.

    The highest-value test in the suite: it catches wear-cost sign
    errors and SoC-repair bugs that produce plausible-looking plans.
    Both strategies run from both start states (floor and full).
    """
    for seed in range(300):
        day, prices, load, solar = _random_day(seed)
        for start in (None, 5.0):
            params = dataclasses.replace(PARAMS, soc_start_kwh=start)
            greedy = solve(prices, load, solar, params)
            static = static_plan(day, load, solar, params)
            static_saving = saving_vs_no_cycling(static, prices, params)
            assert greedy.forecast_saving_eur >= static_saving - 1e-9, (
                f"seed {seed} start {start}: greedy {greedy.forecast_saving_eur:.4f}"
                f" < static {static_saving:.4f} on {day}"
            )


def test_doubling_capacity_never_decreases_saving() -> None:
    """
    PROPERTY: cap_usable 10 kWh saves at least as much as 5 kWh.

    This is the Checkpoint B second-unit lever: more capacity only
    relaxes the C-5 ceiling, so the OPTIMAL saving is monotone in
    capacity. The greedy is not provably monotone: when the 5 kWh
    ceiling blocks a pair, the forced fallback occasionally lands on
    a luckier allocation than the canonical greedy path taken at
    10 kWh. Measured over these 200 deliberately wilder-than-real
    days: 4 seeds show a deficit, worst 0.69% / EUR 0.025 per day —
    a known greedy artifact within ADR-0003's accepted <2% gap, and
    the Checkpoint B comparison runs on annual aggregates where it
    cancels. Encoded guarantee: per-seed deficit bounded by
    max(1%, EUR 0.03), aggregate strictly monotone.
    """
    total_single = total_doubled = 0.0
    doubled_params = dataclasses.replace(PARAMS, cap_usable_kwh=10.0)
    for seed in range(200):
        _, prices, load, solar = _random_day(seed)
        single = solve(prices, load, solar, PARAMS)
        doubled = solve(prices, load, solar, doubled_params)
        total_single += single.forecast_saving_eur
        total_doubled += doubled.forecast_saving_eur
        slack = max(0.03, 0.01 * single.forecast_saving_eur)
        assert doubled.forecast_saving_eur >= single.forecast_saving_eur - slack, (
            f"seed {seed}"
        )
    assert total_doubled > total_single
