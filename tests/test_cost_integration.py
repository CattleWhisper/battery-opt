"""
Integration tests for cost.py's CostTracker (plan Task 13 / decision 8).

Exercises the HA-side wiring directly against the bare `hass` fixture
— CostTracker only needs `hass` and an entity id, no full config-entry
setup — covering delta accumulation across price changes, the meter-
reset floor, transient bad states, and restart persistence via the
Store. `CostTodaySensor`'s own wiring (unavailable without a meter,
its attributes, entry-reload persistence) is covered separately in
test_cost_sensor.py under a full config entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.battery_opt.cost import FIXED_EUR_PER_DAY, CostTracker

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

ENTITY_ID = "sensor.grid_import_energy"


def _set_energy(hass: HomeAssistant, value: float) -> None:
    hass.states.async_set(ENTITY_ID, str(value), {"unit_of_measurement": "kWh"})


async def test_delta_accumulation_across_price_changes(hass: HomeAssistant) -> None:
    """Two deltas priced differently both land in variable_eur."""
    _set_energy(hass, 100.0)
    prices = iter([0.10, 0.20])
    tracker = CostTracker(hass, "entry1", ENTITY_ID, lambda: next(prices))
    await tracker.async_start()

    _set_energy(hass, 101.0)  # +1 kWh at 0.10
    await hass.async_block_till_done()
    _set_energy(hass, 102.5)  # +1.5 kWh at 0.20
    await hass.async_block_till_done()

    assert tracker.state.energy_today_kwh == pytest.approx(2.5)
    assert tracker.state.variable_eur == pytest.approx(1.0 * 0.10 + 1.5 * 0.20)
    assert tracker.state.total_eur == pytest.approx(
        tracker.state.variable_eur + FIXED_EUR_PER_DAY
    )
    tracker.async_stop()


async def test_meter_reset_delta_counts_as_zero(hass: HomeAssistant) -> None:
    """A lower reading (meter reset) contributes neither energy nor cost."""
    _set_energy(hass, 500.0)
    tracker = CostTracker(hass, "entry1", ENTITY_ID, lambda: 0.15)
    await tracker.async_start()

    _set_energy(hass, 10.0)  # meter reset: reading dropped
    await hass.async_block_till_done()

    assert tracker.state.energy_today_kwh == 0.0
    assert tracker.state.variable_eur == 0.0
    tracker.async_stop()


async def test_bad_meter_state_is_ignored_not_a_reset(hass: HomeAssistant) -> None:
    """A transient 'unavailable' reading doesn't corrupt the running delta."""
    _set_energy(hass, 10.0)
    tracker = CostTracker(hass, "entry1", ENTITY_ID, lambda: 0.15)
    await tracker.async_start()

    hass.states.async_set(ENTITY_ID, "unavailable")
    await hass.async_block_till_done()
    _set_energy(hass, 11.0)  # +1 kWh from the last KNOWN reading (10.0)
    await hass.async_block_till_done()

    assert tracker.state.energy_today_kwh == pytest.approx(1.0)
    tracker.async_stop()


async def test_restore_from_store_after_restart(hass: HomeAssistant) -> None:
    """A second tracker instance picks up where the first left off."""
    _set_energy(hass, 100.0)
    first = CostTracker(hass, "entryX", ENTITY_ID, lambda: 0.20)
    await first.async_start()
    _set_energy(hass, 102.0)
    await hass.async_block_till_done()
    assert first.state.energy_today_kwh == pytest.approx(2.0)
    first.async_stop()

    second = CostTracker(hass, "entryX", ENTITY_ID, lambda: 0.20)
    await second.async_start()
    assert second.state.day == first.state.day
    assert second.state.energy_today_kwh == pytest.approx(2.0)
    assert second.state.variable_eur == pytest.approx(first.state.variable_eur)
    second.async_stop()


async def test_different_entries_do_not_share_a_store(hass: HomeAssistant) -> None:
    """Two config entries get independent cost-today Stores."""
    _set_energy(hass, 100.0)
    tracker_a = CostTracker(hass, "entryA", ENTITY_ID, lambda: 0.20)
    await tracker_a.async_start()
    _set_energy(hass, 105.0)
    await hass.async_block_till_done()
    tracker_a.async_stop()

    tracker_b = CostTracker(hass, "entryB", ENTITY_ID, lambda: 0.20)
    await tracker_b.async_start()
    assert tracker_b.state.energy_today_kwh == 0.0
    tracker_b.async_stop()


async def test_on_change_fires_on_each_priced_delta(hass: HomeAssistant) -> None:
    """The on_change callback lets the sensor know to rewrite its state."""
    _set_energy(hass, 100.0)
    calls = 0

    def _on_change() -> None:
        nonlocal calls
        calls += 1

    tracker = CostTracker(hass, "entry1", ENTITY_ID, lambda: 0.15, on_change=_on_change)
    await tracker.async_start()
    _set_energy(hass, 101.0)
    await hass.async_block_till_done()
    assert calls == 1
    tracker.async_stop()
