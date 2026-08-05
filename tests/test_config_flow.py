"""
Config flow and entry setup tests for battery_opt (Task 8).

Runs under pytest-homeassistant-custom-component: `hass` is the real
Home Assistant test harness, so these exercise the actual config
entry lifecycle — form, entry creation, coordinator first refresh
against a fake SoC sensor state, and the options flow.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

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
    coordinator = entry.runtime_data.coordinator
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
            CONF_PRICE_SENSOR: VALID_INPUT[CONF_PRICE_SENSOR],
            CONF_CAPACITY_KWH: 5.0,
            CONF_RESERVE_FLOOR_PCT: 30.0,
            CONF_WEAR_COST: 0.025,
            CONF_PLAN_WEAR: 0.06,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.5)
    assert coordinator.battery_params.wear_cost_eur_kwh == pytest.approx(0.025)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.06)


def _omie_attributes(day_offset: int = 0) -> dict:
    """Build a full hass_omie-shaped day of quarter-hourly data."""
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    day = dt_util.now().date() + timedelta(days=day_offset)
    midnight = datetime(day.year, day.month, day.day, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return {
        "today_hours": {midnight + timedelta(minutes=15 * i): 60.0 for i in range(96)},
    }


async def test_planning_only_without_battery_entities(hass: HomeAssistant) -> None:
    """
    No battery yet: the entry loads, plans and savings are computed.

    The four marstek entities are simply absent; the coordinator reads
    the OMIE sensor and publishes the capped-greedy advisory plan with
    nothing to actuate.
    """
    hass.states.async_set(
        "sensor.omie_spot_price_pt", "60.0", attributes=_omie_attributes()
    )
    data = {
        key: value
        for key, value in VALID_INPUT.items()
        if key
        not in (
            CONF_MODE_SELECT,
            CONF_CHARGE_POWER_NUMBER,
            CONF_DISCHARGE_POWER_NUMBER,
            CONF_SOC_SENSOR,
        )
    }
    data[CONF_PRICE_SENSOR] = "sensor.omie_spot_price_pt"
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.executor is None

    coordinator = entry.runtime_data.coordinator
    assert coordinator.planning_only
    assert coordinator.data["prices_ok"] is True
    assert len(coordinator.data["plan_charge_w"]) == 96
    assert coordinator.data["forecast_saving_eur"] is not None
    assert coordinator.data["vs_static_eur"] is not None

    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.attributes["mode"] == "planning_only"
    assert plan_state.state in ("charge", "discharge", "idle")
    savings = hass.states.get("sensor.battery_opt_forecast_savings")
    assert savings.state not in ("unknown", "unavailable")
    assert hass.states.get("sensor.battery_opt_vs_static") is not None
    healthy = hass.states.get("binary_sensor.battery_opt_healthy")
    assert healthy.state == "on"
    assert "planning only" in healthy.attributes["status"]


async def test_planning_only_without_prices_is_unhealthy(
    hass: HomeAssistant,
) -> None:
    """Planning-only with no OMIE data: loaded but unhealthy, no plan."""
    data = {CONF_PRICE_SENSOR: "sensor.omie_spot_price_pt"}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data["prices_ok"] is False
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "off"
    assert hass.states.get("sensor.battery_opt_forecast_savings").state == "unknown"


async def test_flow_rejects_a_non_omie_price_sensor(hass: HomeAssistant) -> None:
    """A price sensor without OMIE attributes is caught at setup."""
    hass.states.async_set("sensor.omie_spot_price_pt", "0.15")  # wrong shape
    data = {CONF_PRICE_SENSOR: "sensor.omie_spot_price_pt"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **data,
            **{
                k: VALID_INPUT[k]
                for k in (
                    CONF_CAPACITY_KWH,
                    CONF_RESERVE_FLOOR_PCT,
                    CONF_WEAR_COST,
                    CONF_PLAN_WEAR,
                )
            },
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_SENSOR: "price_sensor_not_omie"}


async def test_options_flow_can_fix_the_price_sensor(hass: HomeAssistant) -> None:
    """Re-pointing the price entity via options reloads and takes effect."""
    hass.states.async_set("sensor.omie_correct", "60.0", attributes=_omie_attributes())
    data = {CONF_PRICE_SENSOR: "sensor.omie_wrong_but_absent"}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.data["prices_ok"] is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: "sensor.omie_correct"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # The entry reloaded with the new sensor: plan computed.
    assert entry.runtime_data.coordinator.data["prices_ok"] is True
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "on"


async def test_flow_rejects_partial_battery_entities(hass: HomeAssistant) -> None:
    """One battery entity without the others is a form error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    partial = {
        key: value
        for key, value in VALID_INPUT.items()
        if key not in (CONF_CHARGE_POWER_NUMBER, CONF_DISCHARGE_POWER_NUMBER)
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], partial)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "battery_entities_all_or_none"}


async def test_entities_exist_and_health_follows_the_executor(
    hass: HomeAssistant,
) -> None:
    """Task 9: plan/savings/healthy entities; healthy mirrors ticks."""
    hass.states.async_set("sensor.marstek_soc", "57.0")
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.battery_opt_plan").state == "unknown"
    assert hass.states.get("sensor.battery_opt_forecast_savings") is not None
    healthy = hass.states.get("binary_sensor.battery_opt_healthy")
    assert healthy.state == "off"  # no tick yet
    assert healthy.attributes["status"] == "no tick yet"

    # A quarter-hour tick (winter cheias, 13:00): idle command, healthy on.
    select_calls = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")
    await entry.runtime_data.executor.tick(datetime(2026, 1, 15, 13, 0))
    await hass.async_block_till_done()
    assert len(select_calls) == 1  # ADR-0004: actuation is a service call
    assert select_calls[0].data["option"] == "standby"
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "on"
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.state == "idle"
    assert plan_state.attributes["mode"] == "active"
    assert plan_state.attributes["executor_plan_date"] == "2026-01-15"
    # No OMIE data in this test: the advisory plan is empty, and that
    # is orthogonal to the executor's static actuation.
    assert plan_state.attributes["prices_ok"] is False
