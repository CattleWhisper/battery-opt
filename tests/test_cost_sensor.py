"""
Tests for CostTodaySensor (plan Task 13 / decision 8) under a full config entry.

CostTracker's own accumulation logic is exercised directly in
test_cost_integration.py; this file covers the sensor's wiring:
unavailable without a meter, the fixed term alone, delta accumulation
through the sensor's state, its attributes, and restart persistence
across a config-entry reload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_GRID_ENERGY_SENSOR,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)
from custom_components.battery_opt.cost import FIXED_EUR_PER_DAY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

METER_ENTITY = "sensor.grid_import_energy"


def _set_energy(hass: HomeAssistant, value: float) -> None:
    hass.states.async_set(METER_ENTITY, str(value), {"unit_of_measurement": "kWh"})


async def test_unavailable_without_grid_energy_sensor(hass: HomeAssistant) -> None:
    """Decision 1/8: no CONF_GRID_ENERGY_SENSOR -> the sensor is unavailable."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.battery_opt_cost_today")
    assert state is not None
    assert state.state == "unavailable"


async def test_fixed_term_alone_before_any_energy(hass: HomeAssistant) -> None:
    """With a meter but zero consumption, cost equals the fixed term."""
    _set_energy(hass, 50.0)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_GRID_ENERGY_SENSOR: METER_ENTITY}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.battery_opt_cost_today")
    assert state is not None
    assert state.state != "unavailable"
    assert float(state.state) == pytest.approx(FIXED_EUR_PER_DAY, abs=1e-4)
    assert float(state.attributes["fixed_eur"]) == pytest.approx(FIXED_EUR_PER_DAY)
    assert float(state.attributes["variable_eur"]) == pytest.approx(0.0)
    assert float(state.attributes["energy_today_kwh"]) == pytest.approx(0.0)


async def test_energy_delta_updates_the_sensor_state(hass: HomeAssistant) -> None:
    """
    A meter delta with no priced quarter still updates energy_today_kwh.

    OMIE isn't registered in this test, so cost stays at the fixed term.
    """
    _set_energy(hass, 50.0)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_GRID_ENERGY_SENSOR: METER_ENTITY}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    _set_energy(hass, 52.0)  # +2 kWh, no price known (OMIE not registered)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.battery_opt_cost_today")
    assert float(state.attributes["energy_today_kwh"]) == pytest.approx(2.0)
    assert float(state.attributes["variable_eur"]) == pytest.approx(0.0)
    assert float(state.state) == pytest.approx(FIXED_EUR_PER_DAY, abs=1e-4)


async def test_last_reset_is_local_midnight(hass: HomeAssistant) -> None:
    """state_class TOTAL needs a last_reset; decision 8 pins it to local midnight."""
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    _set_energy(hass, 50.0)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_GRID_ENERGY_SENSOR: METER_ENTITY}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.battery_opt_cost_today")
    assert state.attributes["state_class"] == "total"
    last_reset = dt_util.parse_datetime(state.attributes["last_reset"])
    assert last_reset == dt_util.start_of_local_day(dt_util.now())


async def test_cost_today_persists_across_reload(hass: HomeAssistant) -> None:
    """The Store carries the accumulation across a config-entry reload."""
    _set_energy(hass, 50.0)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_GRID_ENERGY_SENSOR: METER_ENTITY}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    _set_energy(hass, 53.0)  # +3 kWh
    await hass.async_block_till_done()
    before = hass.states.get("sensor.battery_opt_cost_today")
    assert float(before.attributes["energy_today_kwh"]) == pytest.approx(3.0)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = hass.states.get("sensor.battery_opt_cost_today")
    assert float(after.attributes["energy_today_kwh"]) == pytest.approx(3.0)
    assert float(after.state) == pytest.approx(float(before.state))
