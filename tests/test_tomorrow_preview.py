"""
Tests for the D+1 preview (plan Task 10 comfort, decision 9).

`sensor.battery_opt_current_price` gains `tomorrow_prices_eur_kwh`;
`sensor.battery_opt_plan` gains `tomorrow_charge_w` /
`tomorrow_discharge_w`, seeded at the reserve floor rather than
chained from today's (not-yet-executed) plan. Published only when
D+1's own Lisbon day itself builds (market date D+1 available at
all) — structurally always tail-padded, since market date D+2 is
never published this far ahead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)


def _register_core_omie_service(hass: HomeAssistant, days_available: int) -> None:
    """
    Stub HA core's omie.get_prices_for_date service.

    `days_available` market dates from today are served; requesting
    beyond that raises, exactly like the real integration before a
    date is published — used here to control whether D+2 "exists".
    """
    cet = ZoneInfo("Europe/Madrid")
    first_served = dt_util.now().date()

    async def handler(call: ServiceCall) -> dict:
        market_date = call.data["date"]
        if (market_date - first_served).days >= days_available:
            from homeassistant.exceptions import ServiceValidationError  # noqa: PLC0415

            msg = "data_not_available"
            raise ServiceValidationError(msg)
        midnight = datetime(
            market_date.year, market_date.month, market_date.day, tzinfo=cet
        )
        return {
            "pt": [
                {
                    "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                    "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "price": 0.06,
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


async def test_tomorrow_preview_published_when_d_plus_1_is_available(
    hass: HomeAssistant,
) -> None:
    """
    Market date D+1 published (D+2 is not, as always) -> preview builds.

    This is the normal daily case: by the time the 13:45 fetch runs,
    OMIE has published D+1 (tomorrow relative to today), which is
    exactly what tomorrow's OWN Lisbon day needs as ITS "D".
    """
    _register_core_omie_service(hass, days_available=2)  # today, today+1
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["tomorrow_prices_eur_kwh"] is not None
    assert len(coordinator.data["tomorrow_prices_eur_kwh"]) == 96
    # Structurally always tail-padded: D+2 never exists this far ahead.
    assert coordinator.data["tomorrow_prices_padded"] is True
    assert coordinator.data["tomorrow_charge_w"] is not None
    assert coordinator.data["tomorrow_discharge_w"] is not None
    assert len(coordinator.data["tomorrow_charge_w"]) == 96

    price_state = hass.states.get("sensor.battery_opt_current_price")
    assert len(price_state.attributes["tomorrow_prices_eur_kwh"]) == 96
    assert price_state.attributes["tomorrow_prices_padded"] is True
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert len(plan_state.attributes["tomorrow_charge_w"]) == 96
    assert len(plan_state.attributes["tomorrow_discharge_w"]) == 96


async def test_tomorrow_preview_absent_when_d_plus_1_not_yet_published(
    hass: HomeAssistant,
) -> None:
    """Only today's own market date exists -> no tomorrow preview yet."""
    _register_core_omie_service(hass, days_available=1)  # today only
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    # Today itself is padded (no D+1 for today's tail either), but
    # tomorrow cannot build at all: it has zero data for its own "D".
    assert coordinator.data["tomorrow_prices_eur_kwh"] is None
    assert coordinator.data["tomorrow_charge_w"] is None
    assert coordinator.data["tomorrow_discharge_w"] is None

    price_state = hass.states.get("sensor.battery_opt_current_price")
    assert price_state.attributes["tomorrow_prices_eur_kwh"] is None
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.attributes["tomorrow_charge_w"] is None
    assert plan_state.attributes["tomorrow_discharge_w"] is None


async def test_tomorrow_preview_absent_without_omie(hass: HomeAssistant) -> None:
    """No OMIE service at all -> no tomorrow preview, no crash."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["tomorrow_prices_eur_kwh"] is None
    assert coordinator.data["tomorrow_charge_w"] is None
