"""
Config flow and entry setup tests for battery_opt.

Runs under pytest-homeassistant-custom-component: `hass` is the real
Home Assistant test harness. Prices come exclusively from HA core's
OMIE integration, stubbed here as the real `omie.get_prices_for_date`
service with a response in the shape verified from
home-assistant/core sources.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
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
    CONF_RESERVE_FLOOR_PCT,
    CONF_SOC_SENSOR,
    CONF_WEAR_COST,
    DOMAIN,
)

BATTERY_ENTITIES = {
    CONF_MODE_SELECT: "select.marstek_force_mode",
    CONF_CHARGE_POWER_NUMBER: "number.marstek_set_charge_power",
    CONF_DISCHARGE_POWER_NUMBER: "number.marstek_set_discharge_power",
    CONF_SOC_SENSOR: "sensor.marstek_soc",
}

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

VALID_INPUT = {**BATTERY_ENTITIES, **PARAMETERS}


# Mirrors SERVICE_GET_PRICES_SCHEMA in home-assistant/core: `countries`
# coerces to the Country enum, whose values are LOWERCASE "es"/"pt".
# Sending "PT" must fail here exactly as it does in production.
_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)


def _register_core_omie_service(hass: HomeAssistant, days_available: int = 2) -> None:
    """Stub HA core's omie.get_prices_for_date service."""
    cet = ZoneInfo("Europe/Madrid")
    first_served = dt_util.now().date()

    async def handler(call: ServiceCall) -> dict:
        market_date = call.data["date"]
        if (market_date - first_served).days >= days_available:
            msg = "data_not_available"
            raise ServiceValidationError(msg)
        midnight = datetime(
            market_date.year, market_date.month, market_date.day, tzinfo=cet
        )
        return {
            country: [
                {
                    "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                    "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "price": 0.06,
                }
                for i in range(96)
            ]
            for country in call.data["countries"]
            if country == "pt"
        }

    hass.services.async_register(
        "omie",
        "get_prices_for_date",
        handler,
        schema=_OMIE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The single-step form creates a config entry with the input."""
    _register_core_omie_service(hass)
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


async def test_flow_errors_when_omie_not_set_up(hass: HomeAssistant) -> None:
    """Without HA core's OMIE integration there is no price source."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(PARAMETERS)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "omie_not_set_up"}


async def test_flow_rejects_partial_battery_entities(hass: HomeAssistant) -> None:
    """One battery entity without the others is a form error."""
    _register_core_omie_service(hass)
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
    _register_core_omie_service(hass)
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
    coordinator = entry.runtime_data.coordinator
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.5)
    assert coordinator.battery_params.wear_cost_eur_kwh == pytest.approx(0.025)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.06)


async def test_planning_only_computes_plan_from_core_omie(
    hass: HomeAssistant,
) -> None:
    """
    No battery: the entry loads and the advisory plan is computed.

    The battery entities are simply absent; the coordinator pulls the
    day series from the OMIE service and publishes the capped-greedy
    plan with nothing to actuate.
    """
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.executor is None

    coordinator = entry.runtime_data.coordinator
    assert coordinator.planning_only
    assert coordinator.data["prices_ok"] is True
    assert coordinator.data["prices_padded"] is False
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


async def test_core_omie_pads_before_tomorrow_publishes(hass: HomeAssistant) -> None:
    """Only market date D available: the final hour pads, flagged."""
    _register_core_omie_service(hass, days_available=1)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["prices_ok"] is True
    assert coordinator.data["prices_padded"] is True


async def test_planning_only_without_omie_is_unhealthy(
    hass: HomeAssistant,
) -> None:
    """OMIE missing at runtime: loaded but unhealthy, no plan."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data["prices_ok"] is False
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "off"
    assert hass.states.get("sensor.battery_opt_forecast_savings").state == "unknown"


async def test_options_flow_adds_battery_entities_later(
    hass: HomeAssistant,
) -> None:
    """
    When the battery arrives, options bring the executor to life.

    Adding the four entities reloads the entry out of planning-only.
    """
    _register_core_omie_service(hass)
    hass.states.async_set("sensor.marstek_soc", "57.0")
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.executor is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BATTERY_ENTITIES, **PARAMETERS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.executor is not None
    assert not entry.runtime_data.coordinator.planning_only


async def test_all_entities_group_under_one_service_device(
    hass: HomeAssistant,
) -> None:
    """A single service-type device (like core OMIE's) owns everything."""
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    from homeassistant.helpers.device_registry import DeviceEntryType  # noqa: PLC0415

    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.name == "Battery Opt"

    entity_registry = er.async_get(hass)
    grouped = [e for e in entity_registry.entities.values() if e.device_id == device.id]
    assert len(grouped) == 4  # plan, forecast savings, vs static, healthy


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
