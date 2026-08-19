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
    BATTERY_ENTITY_KEYS,
    CONF_BATTERY_POWER_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_CUTOFF_NUMBER,
    CONF_CHARGE_TO_SOC_NUMBER,
    CONF_DISCHARGE_CUTOFF_NUMBER,
    CONF_DRY_RUN,
    CONF_GRID_POWER_SENSOR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_SELF_DISCHARGE_W,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_DRY_RUN,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_SELF_DISCHARGE_W,
    DEVICE_MAX_CHARGE_W,
    SUBENTRY_TYPE_BATTERY,
)
from .fleet import battery_units

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .charge_loop import ChargePowerLoop
    from .coordinator import BatteryOptCoordinator
    from .executor import BatteryOptExecutor
    from .fleet import BatteryUnit

PLATFORMS: list[str] = ["binary_sensor", "button", "sensor", "switch"]

# Config-entry schema version; must match BatteryOptConfigFlow.VERSION.
# v2 = ADR-0009 fleet shape (batteries live in subentries).
_ENTRY_VERSION = 2

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


def _wire_actuation(  # noqa: PLR0913 - one wiring point, many collaborators
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
    merged: dict[str, Any],
    driver: Any,
    coordinator: BatteryOptCoordinator,
    units: list[BatteryUnit],
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
        # Task 12: dry-run (the default) keeps actuation on the static
        # plan; off actuates the coordinator's validated greedy.
        get_dynamic_plan=lambda: coordinator.executor_plan,
        dynamic_enabled=not merged.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
    )

    grid_power_id = merged.get(CONF_GRID_POWER_SENSOR)
    # ADR-0009: the loop needs the COMPLETE fleet draw — every unit
    # must contribute its power sensor, or a unit's charging counts as
    # house load and the setpoints mutually inflate. Legacy entries
    # carry the sensor on the parent (battery_units resolves that).
    battery_power_ids = [unit.power_sensor for unit in units]
    if not (grid_power_id and all(battery_power_ids)):
        return executor, None

    def _power_inputs() -> tuple[float | None, float | None]:
        # The battery sensors follow the HA battery convention
        # (positive = discharging, owner 2026-08-11); the loop's
        # battery_charge_w is charge-positive, so negate the SUM here.
        total_w = 0.0
        for battery_power_id in battery_power_ids:
            battery_w = _read_power_w(hass, battery_power_id)
            if battery_w is None:
                return (_read_power_w(hass, grid_power_id), None)
            total_w += battery_w
        return (_read_power_w(hass, grid_power_id), -total_w)

    charge_loop = ChargePowerLoop(
        driver,
        get_inputs=_power_inputs,
        is_charging=lambda: executor.last_action == "charge",
        p_max_w=DEVICE_MAX_CHARGE_W * len(units),
    )
    loop_holder["loop"] = charge_loop

    async def _on_power_update(_event: Any) -> None:
        await charge_loop.on_update(time.monotonic())

    entry.async_on_unload(
        async_track_state_change_event(hass, [grid_power_id], _on_power_update)
    )
    return executor, charge_loop


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Migrate v1 entries to the ADR-0009 fleet shape (v2).

    The flat battery entity group — plus the per-unit physical
    parameters (capacity, standby self-discharge) and the unit's power
    sensor — moves into a `battery` subentry. The parent keys are left
    in place but ignored from then on (`battery_units`: subentries win
    outright); planning-only entries just get the version bump, and
    their parent-level values remain the no-subentry fallback.
    """
    from types import MappingProxyType  # noqa: PLC0415

    from homeassistant.config_entries import ConfigSubentry  # noqa: PLC0415

    if entry.version > _ENTRY_VERSION:
        return False
    if entry.version < _ENTRY_VERSION:
        merged = {**entry.data, **entry.options}
        if all(merged.get(key) for key in BATTERY_ENTITY_KEYS):
            moved_keys = (
                *BATTERY_ENTITY_KEYS,
                CONF_CHARGE_TO_SOC_NUMBER,
                CONF_CHARGE_CUTOFF_NUMBER,
                CONF_DISCHARGE_CUTOFF_NUMBER,
                CONF_BATTERY_POWER_SENSOR,
            )
            data: dict[str, Any] = {
                key: merged[key] for key in moved_keys if merged.get(key)
            }
            data[CONF_CAPACITY_KWH] = float(
                merged.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)
            )
            data[CONF_SELF_DISCHARGE_W] = float(
                merged.get(CONF_SELF_DISCHARGE_W, DEFAULT_SELF_DISCHARGE_W)
            )
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=SUBENTRY_TYPE_BATTERY,
                    title="Battery",
                    unique_id=None,
                ),
            )
        hass.config_entries.async_update_entry(entry, version=_ENTRY_VERSION)
    return True


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """
    Register domain services once per HA run.

    Registered here rather than per entry so the service survives
    entry reloads and gives a real error (not "service not found")
    when called while no entry is loaded. Import deferred like the
    entry setup's: the package top level must stay HA-free.
    """
    from .services import async_setup_services  # noqa: PLC0415

    async_setup_services(hass)
    return True


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
    from .driver import FleetDriver, MarstekDriver  # noqa: PLC0415

    # Options overlay data: the options flow can re-point entities
    # (e.g. fix the price sensor); batteries are subentries (ADR-0009),
    # resolved by battery_units with the legacy flat group as fallback.
    merged = {**entry.data, **entry.options}
    units = battery_units(entry)
    driver = None
    if units:
        driver = FleetDriver(
            [MarstekDriver(hass, unit.entities) for unit in units],
            [unit.capacity_kwh for unit in units],
            unit_max_w=DEVICE_MAX_CHARGE_W,
        )
        # Spec §8: firmware SOC cutoffs mirror the invariants, written
        # once per unit at setup (EEPROM-backed; compare-before-write
        # inside). Never fatal — the numbers are MISSING on the V3.
        floor_pct = float(merged.get(CONF_RESERVE_FLOOR_PCT, DEFAULT_RESERVE_FLOOR_PCT))
        await driver.write_soc_cutoffs(floor_pct, 100.0)
    coordinator = BatteryOptCoordinator(hass, entry, driver)
    await coordinator.async_restore_load_mae()
    await coordinator.async_restore_greedy_end()
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    executor = None
    charge_loop = None
    if driver is not None:
        executor, charge_loop = _wire_actuation(
            hass, entry, merged, driver, coordinator, units
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
