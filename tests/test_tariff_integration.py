"""
The tariff parameter end to end (ADR-0012): flow, migration, plan, sensors.

`test_tariff.py` and `test_static_tariff.py` prove the core model; this
proves the integration prices and plans with the configured contract,
that a flat contract forces UPS without touching the switch, and that
an entry from before the parameter keeps behaving exactly as it did.

Frozen to a winter Wednesday at 09:00 (Lisbon == UTC in January):
cheias under the weekly tri-horária calendar, with ponta from 09:30.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_FIXED_EUR_DAY,
    CONF_PLAN_WEAR,
    CONF_PRICE_CHEIAS,
    CONF_PRICE_PONTA,
    CONF_PRICE_SIMPLES,
    CONF_PRICE_VAZIO,
    CONF_RESERVE_FLOOR_PCT,
    CONF_TARIFF_CYCLE,
    CONF_TARIFF_OPTION,
    CONF_TARIFF_PRICING,
    CONF_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

PLAN = "sensor.battery_opt_plan"
PRICE = "sensor.battery_opt_current_price"
SWITCH = "switch.battery_opt_ups_mode"
WINTER_WEDNESDAY = "2026-01-14T09:00:00+00:00"

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}
FIXA_SIMPLES = {
    CONF_TARIFF_PRICING: "fixa",
    CONF_TARIFF_OPTION: "simples",
    CONF_TARIFF_CYCLE: "semanal",
    CONF_PRICE_SIMPLES: 0.1420,
    CONF_FIXED_EUR_DAY: 0.4347,
}
FIXA_TRI = {
    CONF_TARIFF_PRICING: "fixa",
    CONF_TARIFF_OPTION: "tri",
    CONF_TARIFF_CYCLE: "semanal",
    CONF_PRICE_VAZIO: 0.1119,
    CONF_PRICE_CHEIAS: 0.1394,
    CONF_PRICE_PONTA: 0.2856,
    CONF_FIXED_EUR_DAY: 0.5717,
}

_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)


def _register_core_omie_service(hass: HomeAssistant) -> list[str]:
    """Stub core OMIE with a mildly varying price; returns the dates asked."""
    cet = ZoneInfo("Europe/Madrid")
    calls: list[str] = []

    async def handler(call: ServiceCall) -> dict:
        market_date = call.data["date"]
        calls.append(market_date.isoformat())
        midnight = datetime(
            market_date.year, market_date.month, market_date.day, tzinfo=cet
        )
        return {
            "pt": [
                {
                    "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                    "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
                    # EUR/kWh; enough intraday swing that Horária simples
                    # has a real spread to arbitrage.
                    "price": 0.02 + 0.10 * ((i // 8) % 2),
                }
                for i in range(96)
            ]
        }

    hass.services.async_register(
        "omie",
        "get_prices_for_date",
        handler,
        schema=_OMIE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return calls


async def _setup(
    hass: HomeAssistant, data: dict, *, omie: bool = True, version: int = 3
) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Lisbon")
    if omie:
        _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=version)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- migration ------------------------------------------------------------


async def test_a_v2_entry_migrates_to_the_pre_adr_0012_contract(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Indexada Horária tri semanal is written in; nothing else changes."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass, dict(PARAMETERS), version=2)
    assert entry.version == 3
    assert entry.data[CONF_TARIFF_PRICING] == "indexada_horaria"
    assert entry.data[CONF_TARIFF_OPTION] == "tri"
    assert entry.data[CONF_TARIFF_CYCLE] == "semanal"
    coordinator = entry.runtime_data.coordinator
    assert coordinator.tariff.key == "indexada_horaria/tri/semanal"
    assert not coordinator.ups_active
    assert max(coordinator.data["plan_discharge_w"]) > 0
    assert hass.states.get(PLAN).attributes["tariff"] == coordinator.tariff.key


# --- flat contracts force UPS ---------------------------------------------


async def test_fixa_simples_holds_the_battery_without_omie(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Fixa simples: no OMIE needed, priced flat, UPS forced by the tariff."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass, {**PARAMETERS, **FIXA_SIMPLES}, omie=False)
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    assert data["prices_ok"] is True
    assert set(data["prices_eur_kwh"]) == {0.1420}
    assert set(data["plan_discharge_w"]) == {0.0}
    assert max(data["plan_charge_w"]) > 0
    plan = hass.states.get(PLAN).attributes
    assert plan["ups_mode"] is True
    assert plan["ups_reason"] == "tariff"
    # The owner's switch is untouched: the tariff is a reason, not a flip.
    assert hass.states.get(SWITCH).state == "off"
    assert coordinator.ups_mode is False
    price = hass.states.get(PRICE)
    assert float(price.state) == pytest.approx(0.1420)
    assert price.attributes["tar_period"] == "simples"


async def test_media_simples_is_flat_and_forces_ups(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Média simples prices every quarter at one estimated average."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(
        hass,
        {
            **PARAMETERS,
            CONF_TARIFF_PRICING: "indexada_media",
            CONF_TARIFF_OPTION: "simples",
            CONF_TARIFF_CYCLE: "semanal",
        },
    )
    data = entry.runtime_data.coordinator.data
    assert len(set(data["prices_eur_kwh"])) == 1
    assert data["prices_estimated"] is True
    assert set(data["plan_discharge_w"]) == {0.0}
    assert hass.states.get(PLAN).attributes["ups_reason"] == "tariff"


async def test_horaria_simples_still_arbitrages_omie(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Horária simples is NOT flat: OMIE moves, so the optimiser runs."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(
        hass,
        {
            **PARAMETERS,
            CONF_TARIFF_PRICING: "indexada_horaria",
            CONF_TARIFF_OPTION: "simples",
            CONF_TARIFF_CYCLE: "semanal",
        },
    )
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.ups_active
    assert len(set(coordinator.data["prices_eur_kwh"])) > 1
    assert hass.states.get(PLAN).attributes["ups_mode"] is False


# --- the switch keeps working on every other tariff -----------------------


async def test_the_switch_still_turns_ups_on_under_a_periodic_tariff(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Owner 2026-11-15: on bi/tri/dynamic the switch can still pick UPS."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass, {**PARAMETERS, **FIXA_TRI}, omie=False)
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.ups_active
    assert max(coordinator.data["plan_discharge_w"]) > 0

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert coordinator.ups_active
    assert set(coordinator.data["plan_discharge_w"]) == {0.0}
    assert hass.states.get(PLAN).attributes["ups_reason"] == "switch"


# --- fixa pricing ---------------------------------------------------------


async def test_fixa_tri_prices_by_period_and_never_calls_omie(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """09:00 on a winter weekday is cheias: the contract's cheias price."""
    freezer.move_to(WINTER_WEDNESDAY)
    await hass.config.async_set_time_zone("Europe/Lisbon")
    calls = _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={**PARAMETERS, **FIXA_TRI}, version=3)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert calls == []
    price = hass.states.get(PRICE)
    assert float(price.state) == pytest.approx(0.1394)
    assert price.attributes["tar_period"] == "cheias"
    prices = set(entry.runtime_data.coordinator.data["prices_eur_kwh"])
    assert prices == {0.1119, 0.1394, 0.2856}


# --- config flow ----------------------------------------------------------


async def test_user_flow_asks_fixa_prices_on_a_second_step(
    hass: HomeAssistant,
) -> None:
    """Fixa needs no OMIE, and asks exactly its option's prices."""
    await hass.config.async_set_time_zone("Europe/Lisbon")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **PARAMETERS,
            CONF_TARIFF_PRICING: "fixa",
            CONF_TARIFF_OPTION: "tri",
            CONF_TARIFF_CYCLE: "diario",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tariff_prices"
    fields = {str(key) for key in result["data_schema"].schema}
    assert fields == {
        CONF_PRICE_VAZIO,
        CONF_PRICE_CHEIAS,
        CONF_PRICE_PONTA,
        CONF_FIXED_EUR_DAY,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PRICE_VAZIO: 0.1119,
            CONF_PRICE_CHEIAS: 0.1394,
            CONF_PRICE_PONTA: 0.2856,
            CONF_FIXED_EUR_DAY: 0.5717,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARIFF_CYCLE] == "diario"
    assert result["data"][CONF_PRICE_PONTA] == pytest.approx(0.2856)


async def test_indexed_flow_still_requires_omie(hass: HomeAssistant) -> None:
    """An indexed contract without OMIE has no price source."""
    await hass.config.async_set_time_zone("Europe/Lisbon")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**PARAMETERS, CONF_TARIFF_PRICING: "indexada_media"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "omie_not_set_up"}


async def test_options_flow_switches_to_fixa_simples(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Changing tariff in the options reloads into the new contract."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass, dict(PARAMETERS), version=2)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **PARAMETERS,
            CONF_TARIFF_PRICING: "fixa",
            CONF_TARIFF_OPTION: "simples",
            CONF_TARIFF_CYCLE: "semanal",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tariff_prices"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PRICE_SIMPLES: 0.1420, CONF_FIXED_EUR_DAY: 0.4347},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.tariff.key == "fixa/simples/-"
    assert coordinator.ups_active
    assert coordinator.tariff.daily_fixed_eur == pytest.approx(0.4347)
