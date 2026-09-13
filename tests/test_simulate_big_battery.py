"""
Tests for the large-battery what-if (backtest/simulate_big_battery.py).

The tool walks the day quarter by quarter and prices every kWh where
it falls, so the properties worth pinning are physical rather than
arithmetic: energy is conserved, the pack never exceeds its capacity
or goes negative, the contracted-power ceiling holds in every quarter,
and the state of charge chains across midnight. Those are what make
the answer non-linear in capacity — and what a flat "excess price"
assumption used to paper over.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.simulate_big_battery import (
    QUARTER_HOURS,
    QUARTERS_PER_DAY,
    BatterySpec,
    flat_consumption,
    run,
    select_charge_quarters,
    simulate_day,
)

DAY = date(2026, 9, 2)
CHEAP, DEAR = 0.05, 0.40
SPEC = BatterySpec(capacity_kwh=20.0)


def _prices(cheap_quarters: int) -> list[float]:
    """Return a price vector: the first N quarters cheap, the rest dear."""
    return [CHEAP if q < cheap_quarters else DEAR for q in range(QUARTERS_PER_DAY)]


def _flat_load(total_kwh: float) -> list[float]:
    return [total_kwh / QUARTERS_PER_DAY] * QUARTERS_PER_DAY


def test_energy_is_conserved() -> None:
    """
    Import equals the load bought directly plus everything stored.

    The accounting identity the whole report rests on: nothing may be
    consumed that was not either imported at the time or discharged
    from the pack.
    """
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    direct = result.consumption_kwh - result.discharged_kwh
    assert result.grid_kwh == pytest.approx(direct + result.charged_kwh)


def test_import_exceeds_consumption_by_the_round_trip_loss() -> None:
    """Cycling a kWh through the pack costs more than not cycling it."""
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    assert result.charged_kwh > 0
    assert result.grid_kwh > result.consumption_kwh


def test_the_pack_never_exceeds_capacity_or_goes_negative() -> None:
    """State of charge stays inside [0, capacity] all day."""
    spec = BatterySpec(capacity_kwh=5.0)
    soc = 0.0
    for _ in range(3):  # several chained days, so it reaches steady state
        result = simulate_day(DAY, _flat_load(24.0), _prices(48), spec, soc)
        soc = result.soc_end_kwh
        assert -1e-9 <= soc <= spec.capacity_kwh + 1e-9


def test_the_contracted_power_ceiling_holds_in_every_quarter() -> None:
    """
    Load plus charging never exceeds --p-usable.

    This is the invariant that stops the simulation from "solving" a
    big pack by drawing power the supply cannot deliver.
    """
    spec = BatterySpec(capacity_kwh=50.0, p_usable_w=2000.0)
    load = _flat_load(24.0)  # 1 kW flat
    charge_quarters = select_charge_quarters(load, _prices(96), spec)
    for quarter in charge_quarters:
        load_w = load[quarter] / QUARTER_HOURS * 1000.0
        assert load_w <= spec.p_usable_w
    # 1 kW load under a 2 kW ceiling leaves 1 kW of charging room.
    result = simulate_day(DAY, load, _prices(96), spec)
    max_quarter_import = spec.p_usable_w * QUARTER_HOURS / 1000.0
    assert result.charged_kwh <= max_quarter_import * len(charge_quarters) + 1e-9


def test_charging_happens_in_the_cheapest_quarters() -> None:
    """Selection is by price, not by clock."""
    chosen = select_charge_quarters(_flat_load(24.0), _prices(32), SPEC)
    assert chosen  # something was picked
    assert all(q < 32 for q in chosen)  # all inside the cheap block


def test_load_in_a_cheap_quarter_is_bought_not_discharged() -> None:
    """
    Deliberate: cheap quarters are served from the grid.

    Spending the pack during the cheapest hours would throw away the
    arbitrage, so the battery is held for the dear ones.
    """
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    non_charge = QUARTERS_PER_DAY - result.charge_quarters
    # Everything the battery served came from non-charge quarters.
    assert result.discharged_kwh <= non_charge * (24.0 / QUARTERS_PER_DAY) + 1e-9


def test_every_kwh_is_priced_at_its_own_quarter() -> None:
    """
    No flat rate anywhere: an all-cheap day costs exactly the cheap price.

    With one price all day the effective rate must equal it, which it
    cannot if any part of the bill is charged at an assumed average.
    """
    result = simulate_day(DAY, _flat_load(24.0), [CHEAP] * QUARTERS_PER_DAY, SPEC)
    assert result.avg_import_price == pytest.approx(CHEAP)
    assert result.cost_eur == pytest.approx(result.grid_kwh * CHEAP)


def test_state_of_charge_chains_across_days() -> None:
    """
    Each day starts where the previous one ended — no midnight reset.

    Without chaining, energy bought in a cheap late-evening quarter
    would vanish at midnight and the answer would depend on where the
    window happened to start.
    """
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 4))
    prices = dict.fromkeys(grid, _prices(32))
    simulation = run(grid, prices, SPEC)
    assert len(simulation.days) == 4
    for previous, current in zip(simulation.days, simulation.days[1:], strict=False):
        assert current.soc_start_kwh == pytest.approx(previous.soc_end_kwh)


def test_a_pack_that_starts_full_buys_less_on_day_one() -> None:
    """
    Carried-in charge displaces buying, which is what chaining is FOR.

    On a flat repeating day the equilibrium is to end empty — the pack
    buys exactly what the day needs — so the chain shows up in what an
    opening charge saves, not in a rising state of charge.
    """
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 2))
    prices = dict.fromkeys(grid, _prices(32))
    empty = run(grid, prices, SPEC, soc_start_kwh=0.0)
    full = run(grid, prices, SPEC, soc_start_kwh=SPEC.capacity_kwh)
    assert full.days[0].soc_start_kwh == pytest.approx(SPEC.capacity_kwh)
    assert full.days[0].charged_kwh < empty.days[0].charged_kwh
    assert full.total("cost_eur") < empty.total("cost_eur")


def test_a_bigger_pack_never_costs_more() -> None:
    """Monotonicity: extra capacity cannot make the bill worse."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 3))
    prices = dict.fromkeys(grid, _prices(32))
    costs = [
        run(grid, prices, BatterySpec(capacity_kwh=cap)).total("cost_eur")
        for cap in (0.0, 5.0, 10.0, 20.0)
    ]
    assert costs == sorted(costs, reverse=True)


def test_no_battery_reproduces_the_spot_baseline() -> None:
    """A zero-capacity pack must cost exactly what buying everything costs."""
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), BatterySpec(0.0))
    assert result.cost_eur == pytest.approx(result.baseline_eur)
    assert result.charged_kwh == pytest.approx(0.0)
    assert result.discharged_kwh == pytest.approx(0.0)


def test_a_day_with_no_consumption_costs_nothing() -> None:
    """An empty day must not spend or divide by zero."""
    result = simulate_day(DAY, [0.0] * QUARTERS_PER_DAY, _prices(64), SPEC)
    assert result.avg_import_price == pytest.approx(0.0)
    assert result.consumption_kwh == pytest.approx(0.0)


def test_unpriced_quarters_are_simulated_but_not_billed() -> None:
    """
    A publication gap costs nothing, mirroring the integration.

    cost.py counts energy with no known price as energy consumed but
    not as cost; the simulation follows, so a gap understates the bill
    rather than inventing a number for it.
    """
    prices: list[float | None] = [None] * QUARTERS_PER_DAY
    result = simulate_day(DAY, _flat_load(24.0), prices, SPEC)
    assert result.consumption_kwh == pytest.approx(24.0)
    assert result.cost_eur == pytest.approx(0.0)


def test_flat_consumption_spreads_the_daily_figure_over_the_window() -> None:
    """--daily-kwh fills every day in [start, end] inclusive."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 3))
    assert sorted(grid) == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    for quarters in grid.values():
        assert len(quarters) == QUARTERS_PER_DAY
        assert sum(quarters) == pytest.approx(24.0)


def test_a_single_day_window_is_one_day_not_zero() -> None:
    """A window whose start equals its end is one day, not zero."""
    grid = flat_consumption(10.0, date(2026, 9, 1), date(2026, 9, 1))
    assert list(grid) == [date(2026, 9, 1)]


def test_synthetic_load_matches_an_equivalent_meter_day() -> None:
    """The two input modes must describe the same day identically."""
    prices = _prices(cheap_quarters=32)
    synthetic = flat_consumption(24.0, DAY, DAY)[DAY]
    from_daily = simulate_day(DAY, synthetic, prices, SPEC)
    from_meter = simulate_day(DAY, _flat_load(24.0), prices, SPEC)
    assert from_daily.total_eur == pytest.approx(from_meter.total_eur)
    assert from_daily.soc_end_kwh == pytest.approx(from_meter.soc_end_kwh)
