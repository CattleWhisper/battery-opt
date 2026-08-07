"""
Coordinator-level tests for Task 10's remainder (plan.md Task 10).

Covers the 13:45/14:15/15:00/16:00 fetch schedule, the static-plan
fallback when prices are unavailable (decision 6), and archiving on a
successful refresh (decision 4).

Runs under pytest-homeassistant-custom-component, reusing the core
OMIE service stub pattern from test_config_flow.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.archive import ARCHIVE_SUBDIR
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

_PRICE_FETCH_TIMES = ((13, 45), (14, 15), (15, 0), (16, 0))

# Mirrors SERVICE_GET_PRICES_SCHEMA in home-assistant/core (see
# test_config_flow.py): `date` coerces from the ISO string HA sends.
_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)


def _register_core_omie_service(hass: HomeAssistant) -> None:
    """Stub HA core's omie.get_prices_for_date service (always succeeds)."""
    cet = ZoneInfo("Europe/Madrid")

    async def handler(call: ServiceCall) -> dict:
        market_date = call.data["date"]
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


async def test_price_fetch_schedule_registers_four_triggers(
    hass: HomeAssistant,
) -> None:
    """
    13:45 plus the three retries are each registered, and each forces a refresh.

    Patches `async_track_time_change` itself rather than jumping mocked
    wall-clock time: HA's internal pattern scheduler computes its next
    match from the real system clock at registration time, which makes
    a wall-clock-jump test flaky depending on when in the day the test
    suite happens to run. Capturing the registered callback and
    invoking it directly is deterministic and exercises the same code
    path (`_on_price_fetch_time` -> `coordinator.async_request_refresh`).
    """
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)

    with patch("homeassistant.helpers.event.async_track_time_change") as mock_track:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # minute=[0, 15, 30, 45] calls (the executor tick / QuarterHourMixin
    # listeners) pass no `hour`, so filtering on hour is not None keeps
    # only the hour-anchored registrations (price fetch + day close).
    registered = {
        (kwargs.get("hour"), kwargs.get("minute")): args[1]
        for args, kwargs in mock_track.call_args_list
        if kwargs.get("hour") is not None
    }
    assert set(_PRICE_FETCH_TIMES) <= set(registered)
    assert (0, 5) in registered  # day-close (Task 11, decision 5)
    assert (0, 0) in registered  # post-midnight refresh (price gap fix)
    assert (2, 0) in registered  # seasonal-switch notification (spec §9)

    coordinator = entry.runtime_data.coordinator
    calls: list[None] = []

    async def _counting_refresh() -> None:
        calls.append(None)

    coordinator.async_request_refresh = AsyncMock(side_effect=_counting_refresh)
    price_fetch_actions = [
        action for time, action in registered.items() if time in _PRICE_FETCH_TIMES
    ]
    for action in price_fetch_actions:
        await action(dt_util.utcnow())
    assert len(calls) == len(_PRICE_FETCH_TIMES)


async def test_no_prices_falls_back_to_static_plan(hass: HomeAssistant) -> None:
    """
    Decision 6: no OMIE service at all -> healthy off, static plan published.

    Mirrors test_planning_only_without_omie_is_unhealthy in
    test_config_flow.py but asserts the plan-sensor fallback contract
    specifically (Task 10's verification criterion).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["prices_ok"] is False
    assert coordinator.data["fallback"] == "static"
    assert len(coordinator.data["plan_charge_w"]) == 96
    assert len(coordinator.data["plan_discharge_w"]) == 96
    assert coordinator.data["forecast_saving_eur"] is None
    assert coordinator.data["vs_static_eur"] is None

    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "off"
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.attributes["fallback"] == "static"
    assert plan_state.state in ("charge", "discharge", "hold")


async def test_successful_refresh_archives_the_day(hass: HomeAssistant) -> None:
    """Decision 4: a successful full-day build writes the archive file."""
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["prices_ok"] is True
    today = dt_util.now().date()
    path = Path(hass.config.path(ARCHIVE_SUBDIR)) / f"{today.isoformat()}.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["date"] == today.isoformat()
    assert len(payload["delivered_eur_kwh"]) == len(coordinator.data["prices_eur_kwh"])
    assert payload["delivered_eur_kwh"] == coordinator.data["prices_eur_kwh"]


async def test_healthy_plan_has_no_fallback_marker(hass: HomeAssistant) -> None:
    """With real prices the plan sensor's fallback attribute is None."""
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.coordinator.data["fallback"] is None
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.attributes["fallback"] is None
