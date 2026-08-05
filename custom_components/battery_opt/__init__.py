"""
Battery Opt — TAR arbitrage planner for the Marstek Venus E 3.0.

Phase 1 shell: config entry setup wires the driver (ADR-0004: service
calls only, never Modbus) and the coordinator. Platforms (sensors,
binary sensor, executor) arrive with Task 9. `core/` stays free of
homeassistant imports (ADR-0001).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_CHARGE_POWER_NUMBER,
    CONF_DISCHARGE_POWER_NUMBER,
    CONF_MODE_SELECT,
    CONF_SOC_SENSOR,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import BatteryOptCoordinator

# Task 9 adds ["sensor", "binary_sensor"].
PLATFORMS: list[str] = []

type BatteryOptConfigEntry = ConfigEntry[BatteryOptCoordinator]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
) -> bool:
    """Wire driver and coordinator for a config entry."""
    # Imported here, not at module top: this package also hosts the
    # HA-free `core/` (ADR-0001), and a submodule import triggers this
    # __init__ — the backtest and core tests must not drag in
    # homeassistant. Guarded by tests/test_ha_free_core.py.
    from .coordinator import BatteryOptCoordinator  # noqa: PLC0415
    from .driver import MarstekDriver, MarstekEntities  # noqa: PLC0415

    entities = MarstekEntities(
        mode_select=entry.data[CONF_MODE_SELECT],
        charge_power_number=entry.data[CONF_CHARGE_POWER_NUMBER],
        discharge_power_number=entry.data[CONF_DISCHARGE_POWER_NUMBER],
        soc_sensor=entry.data[CONF_SOC_SENSOR],
    )
    driver = MarstekDriver(hass, entities)
    coordinator = BatteryOptCoordinator(hass, entry, driver)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: BatteryOptConfigEntry,
) -> bool:
    """Unload the entry's platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
