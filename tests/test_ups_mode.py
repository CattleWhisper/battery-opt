"""
UPS mode end to end: the switch, the coordinator, the published plan.

`test_ups.py` proves the plan builder never discharges; this proves
the rest of the integration actually runs that plan — that flipping
switch.battery_opt_ups_mode republishes the day, flags the plan for
the executor, and leaves the arbitrage bookkeeping it is temporarily
suspending intact.

Frozen to a winter Wednesday: 09:30-12:00 and 18:30-21:00 are ponta,
so the normal plan visibly discharges and "no discharge anywhere" is
a real assertion rather than a property of the calendar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

SWITCH = "switch.battery_opt_ups_mode"
PLAN = "sensor.battery_opt_plan"
# Lisbon == UTC in January, so the frozen instant is also local time.
WINTER_WEDNESDAY = "2026-01-14T09:00:00+00:00"

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


def _register_core_omie_service(hass: HomeAssistant) -> None:
    """Stub core OMIE with a flat commodity price, so TAR sets the spread."""
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
                    # EUR/kWh, the unit core OMIE's service reports
                    # (same convention as tests/test_coordinator.py):
                    # ~0.075 delivered, so the TAR spread dominates and
                    # C-8 pairing is comfortably profitable.
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


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Lisbon")
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set_ups(hass: HomeAssistant, *, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_the_normal_plan_discharges(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The control: without the switch, this day sells into ponta."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    assert max(entry.runtime_data.coordinator.data["plan_discharge_w"]) > 0


async def test_turning_on_ups_mode_removes_every_discharge(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The owner's actual request, measured on the published plan."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    await _set_ups(hass, on=True)

    data = entry.runtime_data.coordinator.data
    assert set(data["plan_discharge_w"]) == {0.0}
    assert max(data["plan_charge_w"]) > 0  # still refilling the reserve


async def test_the_published_plan_is_flagged_for_the_executor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    Without the flag the executor could not tell the two plans apart.

    It refuses to actuate a published plan whose flag disagrees with
    its own live switch reading — that is what closes the window
    between flipping the switch and the next coordinator refresh.
    """
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.executor_plan is not None
    assert coordinator.executor_plan.ups is False

    await _set_ups(hass, on=True)
    assert coordinator.executor_plan is not None
    assert coordinator.executor_plan.ups is True
    assert set(coordinator.executor_plan.plan.discharge_w) == {0.0}


async def test_the_arbitrage_baseline_is_still_published_and_priced(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    UPS mode costs money; the dashboard has to be able to say how much.

    The static schedule keeps being published beside the UPS plan, and
    `vs_static_eur` is negative — the arbitrage given up, not a saving.
    """
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    await _set_ups(hass, on=True)

    data = entry.runtime_data.coordinator.data
    assert max(data["static_discharge_w"]) > 0
    assert data["vs_static_eur"] < 0


async def test_tomorrows_preview_is_also_a_ups_plan(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """D+1 has to agree with today, or the dashboard contradicts itself."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    await _set_ups(hass, on=True)

    data = entry.runtime_data.coordinator.data
    assert data["tomorrow_discharge_w"] is not None
    assert set(data["tomorrow_discharge_w"]) == {0.0}
    assert max(data["tomorrow_static_discharge_w"]) > 0


async def test_the_plan_sensor_reports_the_mode(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An automation or dashboard can see the mode without guessing."""
    freezer.move_to(WINTER_WEDNESDAY)
    await _setup(hass)
    assert hass.states.get(PLAN).attributes["ups_mode"] is False

    await _set_ups(hass, on=True)
    assert hass.states.get(PLAN).attributes["ups_mode"] is True


async def test_turning_it_off_restores_arbitrage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The switch is symmetric — the mode is temporary by design."""
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    await _set_ups(hass, on=True)
    await _set_ups(hass, on=False)

    data = entry.runtime_data.coordinator.data
    assert max(data["plan_discharge_w"]) > 0
    assert entry.runtime_data.coordinator.executor_plan.ups is False


async def test_a_ups_day_does_not_advance_the_greedy_chain(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    The arbitrage battery's trajectory is not the UPS battery's.

    The greedy chains its own end across days. A UPS day ends
    somewhere near full and has nothing to say about where the greedy
    would have been, so recording it would corrupt the chain the
    moment the owner switched back. Observable as the greedy's start
    SoC surviving the round trip unchanged.
    """
    freezer.move_to(WINTER_WEDNESDAY)
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    before = coordinator.data["plan_soc_kwh"][0]

    await _set_ups(hass, on=True)
    await _set_ups(hass, on=False)

    assert coordinator.data["plan_soc_kwh"][0] == before


async def test_ups_mode_survives_a_restart(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    A storm does not end because Home Assistant restarted.

    The switch restores its own state, and restoring ON sets the
    coordinator flag before anything can tick — so no plan built after
    the restart can discharge the reserve.
    """
    from homeassistant.core import State  # noqa: PLC0415

    freezer.move_to(WINTER_WEDNESDAY)
    mock_restore_cache(hass, (State(SWITCH, "on"),))
    entry = await _setup(hass)

    assert hass.states.get(SWITCH).state == "on"
    coordinator = entry.runtime_data.coordinator
    assert coordinator.ups_mode is True
    assert set(coordinator.data["plan_discharge_w"]) == {0.0}
