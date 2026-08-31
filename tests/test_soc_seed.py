"""
The measured midnight seed reaching the plan (ADR-0010).

`test_soc_sync.py` covers the read guards; this covers the wiring:
that a fresh post-midnight reading becomes the day's start SoC, that
it is captured once and then pinned, and that every refusal path
leaves the modelled chain untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_SOC_SENSOR,
    CONF_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

SOC = "sensor.battery_soc"

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

# Lisbon == UTC in January, so the frozen instant is also local time.
_JUST_AFTER_MIDNIGHT = "2026-01-14T00:10:00+00:00"
_AFTERNOON = "2026-01-14T14:00:00+00:00"


async def _setup(hass: HomeAssistant, *, with_soc_sensor: bool) -> MockConfigEntry:
    # Production requires Europe/Lisbon (config_flow.REQUIRED_TIME_ZONE);
    # the test harness defaults to US/Pacific, which would put the
    # frozen instant outside the post-midnight capture window.
    await hass.config.async_set_time_zone("Europe/Lisbon")
    data = dict(PARAMETERS)
    if with_soc_sensor:
        data[CONF_SOC_SENSOR] = SOC
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _coordinator(entry: MockConfigEntry):  # noqa: ANN202 - test helper
    return entry.runtime_data.coordinator


async def test_fresh_reading_becomes_the_days_seed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """44% of 5 kWh seeds the day at 2.2 kWh, not the modelled chain."""
    freezer.move_to(_JUST_AFTER_MIDNIGHT)
    hass.states.async_set(SOC, "44")
    entry = await _setup(hass, with_soc_sensor=True)

    coordinator = _coordinator(entry)
    seed = coordinator.measured_start_soc_kwh(coordinator.data["plan_date"])
    assert seed == pytest.approx(2.2)
    # And it is what the published static trajectory actually starts from.
    assert coordinator.data["static_soc_kwh"][0] == pytest.approx(2.2, abs=1e-3)


async def test_seed_is_clamped_into_the_modelled_envelope(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    A reading below the reserve floor still seeds AT the floor.

    The real battery may sit under the planning floor (ADR-0008: the
    firmware minimum is lower), but a plan seeded below C-4 would be
    out of bounds from its first interval.
    """
    freezer.move_to(_JUST_AFTER_MIDNIGHT)
    hass.states.async_set(SOC, "5")  # 0.25 kWh, under the 27% = 1.35 floor
    entry = await _setup(hass, with_soc_sensor=True)

    coordinator = _coordinator(entry)
    seed = coordinator.measured_start_soc_kwh(coordinator.data["plan_date"])
    assert seed == pytest.approx(coordinator.battery_params.cap_min_kwh)


async def test_no_sensor_configured_changes_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The feature is opt-in: without the sensor the model is untouched."""
    freezer.move_to(_JUST_AFTER_MIDNIGHT)
    entry = await _setup(hass, with_soc_sensor=False)

    coordinator = _coordinator(entry)
    assert coordinator.measured_start_soc_kwh(coordinator.data["plan_date"]) is None


async def test_a_stale_sensor_falls_back_to_the_model(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """ADR-0008's frozen-reading failure: refuse it, keep the chain."""
    freezer.move_to("2026-01-13T20:00:00+00:00")
    hass.states.async_set(SOC, "44")  # reported four hours before midnight
    freezer.move_to(_JUST_AFTER_MIDNIGHT)
    entry = await _setup(hass, with_soc_sensor=True)

    coordinator = _coordinator(entry)
    assert coordinator.measured_start_soc_kwh(coordinator.data["plan_date"]) is None


async def test_outside_the_window_nothing_is_captured(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An afternoon SoC is not the day's START and must not seed it."""
    freezer.move_to(_AFTERNOON)
    hass.states.async_set(SOC, "44")
    entry = await _setup(hass, with_soc_sensor=True)

    coordinator = _coordinator(entry)
    assert coordinator.measured_start_soc_kwh(coordinator.data["plan_date"]) is None


async def test_captured_seed_is_pinned_for_the_rest_of_the_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    Captured once at midnight, then served unchanged.

    The owner asked for a midnight sync, not intraday re-anchoring:
    once the day has started the seed must not follow the battery
    around, or every refresh would rewrite the morning.
    """
    freezer.move_to(_JUST_AFTER_MIDNIGHT)
    hass.states.async_set(SOC, "44")
    entry = await _setup(hass, with_soc_sensor=True)
    coordinator = _coordinator(entry)
    day = coordinator.data["plan_date"]
    assert coordinator.measured_start_soc_kwh(day) == pytest.approx(2.2)

    # The battery drains through the day and the window closes.
    freezer.move_to(_AFTERNOON)
    hass.states.async_set(SOC, "12")
    assert coordinator.measured_start_soc_kwh(day) == pytest.approx(2.2)
