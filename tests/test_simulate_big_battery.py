"""
Tests for the large-battery what-if (backtest/simulate_big_battery.py).

The tool answers a sizing question, so the properties worth pinning are
the ones that make the answer non-linear in capacity: round-trip losses
inflate the purchase, the contracted-power ceiling caps how much of a
cheap window is reachable, and a pack bigger than the day's own
consumption buys nothing extra.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.simulate_big_battery import (
    QUARTERS_PER_DAY,
    BatterySpec,
    simulate_day,
)

DAY = date(2026, 9, 2)
CHEAP, DEAR = 0.05, 0.40


def _prices(cheap_quarters: int) -> list[float]:
    """Return a price vector: the first N quarters cheap, the rest dear."""
    return [CHEAP if q < cheap_quarters else DEAR for q in range(QUARTERS_PER_DAY)]


def _flat_load(total_kwh: float) -> list[float]:
    return [total_kwh / QUARTERS_PER_DAY] * QUARTERS_PER_DAY


def test_charges_in_the_cheapest_quarters_first() -> None:
    """The whole point: the fill order is by price, not by clock."""
    result = simulate_day(
        DAY,
        _flat_load(24.0),
        _prices(cheap_quarters=32),
        BatterySpec(capacity_kwh=10.0, excess_price=0.25),
    )
    # 10 kWh served needs 11.11 kWh bought; the cheap block holds far
    # more than that, so every kWh must be bought at the cheap price.
    assert result.avg_charge_price == pytest.approx(CHEAP)
    assert result.charged_kwh == pytest.approx(10.0 / 0.9)


def test_round_trip_losses_inflate_the_purchase() -> None:
    """Delivering X kWh costs X/eta at the meter, never X."""
    spec = BatterySpec(capacity_kwh=9.0, excess_price=0.25, eta_roundtrip=0.9)
    result = simulate_day(DAY, _flat_load(24.0), _prices(64), spec)
    assert result.served_kwh == pytest.approx(9.0)
    assert result.charged_kwh == pytest.approx(10.0)


def test_capacity_beyond_the_days_consumption_buys_nothing() -> None:
    """A pack larger than the day's use is not charged past what it serves."""
    load = _flat_load(12.0)
    small = simulate_day(
        DAY, load, _prices(64), BatterySpec(capacity_kwh=12.0, excess_price=0.25)
    )
    huge = simulate_day(
        DAY, load, _prices(64), BatterySpec(capacity_kwh=500.0, excess_price=0.25)
    )
    assert huge.served_kwh == pytest.approx(small.served_kwh) == pytest.approx(12.0)
    assert huge.total_eur == pytest.approx(small.total_eur)
    assert huge.excess_kwh == pytest.approx(0.0)


def test_the_contracted_power_ceiling_caps_what_can_be_charged() -> None:
    """
    Headroom is the ceiling MINUS that quarter's own load.

    With a 1500 W ceiling against a flat 1 kW house load, only 500 W of
    charging fits in any quarter: 96 x 500 W x 0.25 h = 12 kWh bought,
    10.8 kWh delivered, and the remainder spills to the excess price.
    """
    result = simulate_day(
        DAY,
        _flat_load(24.0),  # 1 kW flat
        _prices(96),
        BatterySpec(capacity_kwh=20.0, excess_price=0.25, p_usable_w=1500.0),
    )
    assert result.charged_kwh == pytest.approx(12.0)
    assert result.served_kwh == pytest.approx(10.8)
    assert result.excess_kwh == pytest.approx(24.0 - 10.8)


def test_uncovered_energy_is_priced_at_the_flat_excess_rate() -> None:
    """Whatever the battery cannot serve is bought at --excess-price."""
    spec = BatterySpec(capacity_kwh=5.0, excess_price=0.25)
    result = simulate_day(DAY, _flat_load(24.0), _prices(64), spec)
    assert result.excess_kwh == pytest.approx(19.0)
    assert result.excess_cost_eur == pytest.approx(19.0 * 0.25)
    assert result.total_eur == pytest.approx(result.charge_cost_eur + 19.0 * 0.25)


def test_a_day_with_no_consumption_costs_nothing() -> None:
    """An empty day must not charge, spend, or divide by zero."""
    result = simulate_day(
        DAY,
        [0.0] * QUARTERS_PER_DAY,
        _prices(64),
        BatterySpec(capacity_kwh=20.0, excess_price=0.25),
    )
    assert result.total_eur == pytest.approx(0.0)
    assert result.charged_kwh == pytest.approx(0.0)
    assert result.avg_charge_price == pytest.approx(0.0)


def test_missing_prices_do_not_block_the_rest_of_the_day() -> None:
    """A partial price vector is usable; unpriced quarters are skipped."""
    prices: list[float | None] = _prices(64)
    prices[:8] = [None] * 8  # e.g. a DST-short day or a publication gap
    result = simulate_day(
        DAY,
        _flat_load(24.0),
        prices,
        BatterySpec(capacity_kwh=10.0, excess_price=0.25),
    )
    assert result.charged_kwh == pytest.approx(10.0 / 0.9)
    assert result.avg_charge_price == pytest.approx(CHEAP)
