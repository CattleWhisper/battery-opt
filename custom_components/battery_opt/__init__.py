"""
Battery Opt — TAR arbitrage planner for the Marstek Venus E 3.0.

Phase 1 shell: config entry setup wires the driver (ADR-0004: service
calls only, never Modbus), the coordinator, and the 15-minute
executor running the static plan. `core/` stays free of homeassistant
imports (ADR-0001).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_CHARGE_CUTOFF_NUMBER,
    CONF_CHARGE_POWER_NUMBER,
    CONF_CHARGE_TO_SOC_NUMBER,
    CONF_DISCHARGE_CUTOFF_NUMBER,
    CONF_GRID_POWER_SENSOR,
    CONF_MODE_SELECT,
    CONF_RESERVE_FLOOR_PCT,
    CONF_RS485_SWITCH,
    CONF_WORK_MODE_SELECT,
    DEFAULT_RESERVE_FLOOR_PCT,
)

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .charge_loop import ChargePowerLoop
    from .coordinator import BatteryOptCoordinator
    from .executor import BatteryOptExecutor

PLATFORMS: list[str] = ["binary_sensor", "button", "sensor", "switch"]

# Plan Task 10 / spec §9: aligned fetch at 13:45 local (OMIE D+1
# publishes ~13:30), retried at 14:15, 15:00 and 16:00 in case it was
# not yet published. Each entry just forces a coordinator refresh —
# the 15-minute steady-state poll already covers everything else, so
# a fetch that already succeeded is a harmless extra refresh. The
# config flow requires HA's timezone to be Europe/Lisbon, so "local"
# here IS Lisbon.
_PRICE_FETCH_TIMES: tuple[tuple[int, int], ...] = (
    (13, 45),
    (14, 15),
    (15, 0),
    (16, 0),
)

# Just past midnight: today's price vector exists (market date D was
# published yesterday ~13:30) but the coordinator's steady 15-minute
# poll can lag the date change by up to a full interval, leaving the
# current-price sensor (and the cost tracker's price lookup) blank for
# those minutes every night. Second 30 avoids racing the cost
# tracker's own 00:00:00 day roll.
_MIDNIGHT_REFRESH = (0, 0, 30)


@dataclass
class BatteryOptRuntime:
    """
    Everything the platforms need from a loaded entry.

    `executor` is None in planning-only mode (no battery configured):
    plans are computed and published, nothing actuates. `charge_loop`
    is None until both ADR-0007 power sensors are configured.
    """

    coordinator: BatteryOptCoordinator
    executor: BatteryOptExecutor | None
    charge_loop: ChargePowerLoop | None = None


def _read_power_w(hass: HomeAssistant, entity_id: str) -> float | None:
    """Parse a power sensor state; None when unavailable/non-numeric."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


type BatteryOptConfigEntry = ConfigEntry[BatteryOptRuntime]


def _wire_actuation(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
    merged: dict[str, Any],
    driver: Any,
    coordinator: BatteryOptCoordinator,
) -> tuple[BatteryOptExecutor, ChargePowerLoop | None]:
    """Build the executor and, when its sensors exist, the charge loop."""
    from homeassistant.helpers.event import (  # noqa: PLC0415
        async_track_state_change_event,
    )

    from .charge_loop import CHARGE_FALLBACK_W, ChargePowerLoop  # noqa: PLC0415
    from .executor import BatteryOptExecutor  # noqa: PLC0415

    def _notify(message: str) -> None:
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification",
                "create",
                {"title": "Battery Opt", "message": message},
            )
        )

    # ADR-0007: executor and loop reference each other (entry setpoint
    # one way, is-charging the other) — late-bind the loop through a
    # holder so both can be constructed.
    loop_holder: dict[str, ChargePowerLoop | None] = {"loop": None}

    def _charge_entry_w() -> float:
        loop = loop_holder["loop"]
        return CHARGE_FALLBACK_W if loop is None else loop.entry_setpoint_w()

    def _charge_entry_written(watts: float) -> None:
        loop = loop_holder["loop"]
        if loop is not None:
            loop.mark_written(watts, time.monotonic())

    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: coordinator.battery_params,
        notify=_notify,
        get_charge_entry_w=_charge_entry_w,
        on_charge_entry=_charge_entry_written,
    )

    grid_power_id = merged.get(CONF_GRID_POWER_SENSOR)
    battery_power_id = merged.get(CONF_BATTERY_POWER_SENSOR)
    if not (grid_power_id and battery_power_id):
        return executor, None

    def _power_inputs() -> tuple[float | None, float | None]:
        # The battery sensor follows the HA battery convention
        # (positive = discharging, owner 2026-08-11); the loop's
        # battery_charge_w is charge-positive, so negate here.
        battery_w = _read_power_w(hass, battery_power_id)
        return (
            _read_power_w(hass, grid_power_id),
            None if battery_w is None else -battery_w,
        )

    charge_loop = ChargePowerLoop(
        driver,
        get_inputs=_power_inputs,
        is_charging=lambda: executor.last_action == "charge",
    )
    loop_holder["loop"] = charge_loop

    async def _on_power_update(_event: Any) -> None:
        await charge_loop.on_update(time.monotonic())

    entry.async_on_unload(
        async_track_state_change_event(hass, [grid_power_id], _on_power_update)
    )
    return executor, charge_loop


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
) -> bool:
    """Wire driver, coordinator and executor for a config entry."""
    # Imported here, not at module top: this package also hosts the
    # HA-free `core/` (ADR-0001), and a submodule import triggers this
    # __init__ — the backtest and core tests must not drag in
    # homeassistant. Guarded by tests/test_ha_free_core.py.
    from homeassistant.helpers.event import async_track_time_change  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from .coordinator import BatteryOptCoordinator  # noqa: PLC0415
    from .core.calendar import season_switch  # noqa: PLC0415
    from .driver import MarstekDriver, MarstekEntities  # noqa: PLC0415

    # Options overlay data: the options flow can re-point entities
    # (e.g. fix the price sensor, add the battery when it arrives).
    merged = {**entry.data, **entry.options}
    battery_keys = (
        CONF_MODE_SELECT,
        CONF_CHARGE_POWER_NUMBER,
        CONF_RS485_SWITCH,
        CONF_WORK_MODE_SELECT,
    )
    has_battery = all(merged.get(key) for key in battery_keys)
    driver = None
    if has_battery:
        entities = MarstekEntities(
            mode_select=merged[CONF_MODE_SELECT],
            charge_power_number=merged[CONF_CHARGE_POWER_NUMBER],
            rs485_switch=merged[CONF_RS485_SWITCH],
            work_mode_select=merged[CONF_WORK_MODE_SELECT],
            charge_to_soc_number=merged.get(CONF_CHARGE_TO_SOC_NUMBER),
            charge_cutoff_number=merged.get(CONF_CHARGE_CUTOFF_NUMBER),
            discharge_cutoff_number=merged.get(CONF_DISCHARGE_CUTOFF_NUMBER),
        )
        driver = MarstekDriver(hass, entities)
        # Spec §8: firmware SOC cutoffs mirror the invariants, written
        # once at setup (EEPROM-backed; compare-before-write inside).
        # Never fatal — the numbers are MISSING on the Venus E V3.
        floor_pct = float(merged.get(CONF_RESERVE_FLOOR_PCT, DEFAULT_RESERVE_FLOOR_PCT))
        await driver.write_soc_cutoffs(floor_pct, 100.0)
    coordinator = BatteryOptCoordinator(hass, entry, driver)
    await coordinator.async_restore_load_mae()
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    executor = None
    charge_loop = None
    if driver is not None:
        executor, charge_loop = _wire_actuation(
            hass, entry, merged, driver, coordinator
        )
    entry.runtime_data = BatteryOptRuntime(
        coordinator=coordinator, executor=executor, charge_loop=charge_loop
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _on_price_fetch_time(_now: datetime) -> None:
        await coordinator.async_request_refresh()

    for hour, minute in _PRICE_FETCH_TIMES:
        entry.async_on_unload(
            async_track_time_change(
                hass, _on_price_fetch_time, hour=hour, minute=minute, second=0
            )
        )
    midnight_hour, midnight_minute, midnight_second = _MIDNIGHT_REFRESH
    entry.async_on_unload(
        async_track_time_change(
            hass,
            _on_price_fetch_time,
            hour=midnight_hour,
            minute=midnight_minute,
            second=midnight_second,
        )
    )

    async def _on_day_close(now: datetime) -> None:
        await coordinator.async_day_close(dt_util.as_local(now))

    entry.async_on_unload(
        async_track_time_change(hass, _on_day_close, hour=0, minute=5, second=0)
    )

    async def _on_seasonal_check(now: datetime) -> None:
        # Spec §9: the two season-switch days a year get a manual-
        # verification prompt — the calendar is the #1 silent trap.
        new_season = season_switch(dt_util.as_local(now).date())
        if new_season is None:
            return
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Battery Opt: tariff season switch",
                "message": (
                    f"Today is the last Sunday of the month: the tri-horária "
                    f"calendar switches to {new_season} hours. Verify the "
                    f"plan's charge/discharge windows against the new "
                    f"season (spec §9)."
                ),
            },
        )

    entry.async_on_unload(
        async_track_time_change(hass, _on_seasonal_check, hour=2, minute=0, second=0)
    )

    if executor is not None:
        actuator = executor

        async def _on_quarter_hour(now: datetime) -> None:
            await actuator.tick(dt_util.as_local(now))

        entry.async_on_unload(
            async_track_time_change(
                hass, _on_quarter_hour, minute=[0, 15, 30, 45], second=0
            )
        )
    return True


async def _async_options_updated(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
) -> None:
    """Reload on options change so entity re-selection takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
) -> bool:
    """Unload the entry's platforms; the timer dies with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
