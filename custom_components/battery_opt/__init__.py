"""
Battery Opt — TAR arbitrage planner for the Marstek Venus E 3.0.

Phase 1 shell: config entry setup wires the driver (ADR-0004: service
calls only, never Modbus), the coordinator, and the 15-minute
executor running the static plan. `core/` stays free of homeassistant
imports (ADR-0001).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import (
    CONF_CHARGE_POWER_NUMBER,
    CONF_DISCHARGE_POWER_NUMBER,
    CONF_MODE_SELECT,
    CONF_SOC_SENSOR,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import BatteryOptCoordinator
    from .executor import BatteryOptExecutor

PLATFORMS: list[str] = ["binary_sensor", "sensor"]

# Plan Task 10 / spec §9: aligned fetch at 13:45 Lisbon local (OMIE D+1
# publishes ~13:30), retried at 14:15, 15:00 and 16:00 in case it was
# not yet published. Each entry just forces a coordinator refresh —
# the 15-minute steady-state poll already covers everything else, so
# a fetch that already succeeded is a harmless extra refresh.
_PRICE_FETCH_TIMES: tuple[tuple[int, int], ...] = (
    (13, 45),
    (14, 15),
    (15, 0),
    (16, 0),
)


@dataclass
class BatteryOptRuntime:
    """
    Everything the platforms need from a loaded entry.

    `executor` is None in planning-only mode (no battery configured):
    plans are computed and published, nothing actuates.
    """

    coordinator: BatteryOptCoordinator
    executor: BatteryOptExecutor | None


type BatteryOptConfigEntry = ConfigEntry[BatteryOptRuntime]


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
    from .driver import MarstekDriver, MarstekEntities  # noqa: PLC0415
    from .executor import BatteryOptExecutor  # noqa: PLC0415

    # Options overlay data: the options flow can re-point entities
    # (e.g. fix the price sensor, add the battery when it arrives).
    merged = {**entry.data, **entry.options}
    battery_keys = (
        CONF_MODE_SELECT,
        CONF_CHARGE_POWER_NUMBER,
        CONF_DISCHARGE_POWER_NUMBER,
        CONF_SOC_SENSOR,
    )
    has_battery = all(merged.get(key) for key in battery_keys)
    driver = None
    if has_battery:
        entities = MarstekEntities(
            mode_select=merged[CONF_MODE_SELECT],
            charge_power_number=merged[CONF_CHARGE_POWER_NUMBER],
            discharge_power_number=merged[CONF_DISCHARGE_POWER_NUMBER],
            soc_sensor=merged[CONF_SOC_SENSOR],
        )
        driver = MarstekDriver(hass, entities)
    coordinator = BatteryOptCoordinator(hass, entry, driver)
    await coordinator.async_restore_load_mae()
    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    executor = None
    if driver is not None:

        def _notify(message: str) -> None:
            hass.async_create_task(
                hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {"title": "Battery Opt", "message": message},
                )
            )

        executor = BatteryOptExecutor(
            driver=driver,
            get_params=lambda: coordinator.battery_params,
            get_soc_kwh=lambda: (coordinator.data or {}).get("soc_kwh"),
            notify=_notify,
        )
    entry.runtime_data = BatteryOptRuntime(coordinator=coordinator, executor=executor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _on_price_fetch_time(_now: datetime) -> None:
        await coordinator.async_request_refresh()

    for hour, minute in _PRICE_FETCH_TIMES:
        entry.async_on_unload(
            async_track_time_change(
                hass, _on_price_fetch_time, hour=hour, minute=minute, second=0
            )
        )

    async def _on_day_close(now: datetime) -> None:
        await coordinator.async_day_close(dt_util.as_local(now))

    entry.async_on_unload(
        async_track_time_change(hass, _on_day_close, hour=0, minute=5, second=0)
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
