"""
Config flow and entry setup tests for battery_opt (Task 8).

Runs under pytest-homeassistant-custom-component: `hass` is the real
Home Assistant test harness, so these exercise the actual config
entry lifecycle — form, entry creation, coordinator first refresh
against a fake SoC sensor state, and the options flow.
"""

from typing import TYPE_CHECKING

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_POWER_NUMBER,
    CONF_DISCHARGE_POWER_NUMBER,
    CONF_MODE_SELECT,
    CONF_PLAN_WEAR,
    CONF_PRICE_SENSOR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_SOC_SENSOR,
    CONF_WEAR_COST,
    DOMAIN,
)

VALID_INPUT = {
    CONF_MODE_SELECT: "select.marstek_force_mode",
    CONF_CHARGE_POWER_NUMBER: "number.marstek_set_charge_power",
    CONF_DISCHARGE_POWER_NUMBER: "number.marstek_set_discharge_power",
    CONF_SOC_SENSOR: "sensor.marstek_soc",
    CONF_PRICE_SENSOR: "sensor.omie_spot_price",
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The single-step form creates a config entry with the input."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Battery Opt"
    assert result["data"] == VALID_INPUT


async def test_setup_entry_polls_soc_through_the_driver(
    hass: HomeAssistant,
) -> None:
    """First refresh reads the SoC sensor; params come from the entry."""
    hass.states.async_set("sensor.marstek_soc", "57.0")
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.data["soc_percent"] == pytest.approx(57.0)
    assert coordinator.data["soc_kwh"] == pytest.approx(2.85)
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.35)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.0467)


async def test_setup_retries_when_soc_unavailable(hass: HomeAssistant) -> None:
    """No SoC state yet: the entry goes to setup-retry, not loaded."""
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_flow_edits_parameters(hass: HomeAssistant) -> None:
    """Numeric parameters are editable afterwards and take effect."""
    hass.states.async_set("sensor.marstek_soc", "57.0")
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CAPACITY_KWH: 5.0,
            CONF_RESERVE_FLOOR_PCT: 30.0,
            CONF_WEAR_COST: 0.025,
            CONF_PLAN_WEAR: 0.06,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.5)
    assert coordinator.battery_params.wear_cost_eur_kwh == pytest.approx(0.025)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.06)
