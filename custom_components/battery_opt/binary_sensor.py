"""
Health binary sensor (spec §8): on = safe to actuate.

Off on missing SoC, an invalid plan, or a three-strike driver failure
— exactly the executor's health latch. The executor never actuates
while this is off (spec §11); the sensor only reports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BatteryOptConfigEntry
    from .executor import BatteryOptExecutor


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA-required signature
    entry: BatteryOptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the health sensor."""
    async_add_entities([HealthySensor(entry.runtime_data.executor, entry.entry_id)])


class HealthySensor(BinarySensorEntity):
    """Mirrors the executor's health latch."""

    _attr_name = "Battery Opt Healthy"
    _attr_suggested_object_id = "battery_opt_healthy"
    _attr_should_poll = False
    _attr_icon = "mdi:heart-pulse"

    def __init__(self, executor: BatteryOptExecutor, entry_id: str) -> None:
        """Bind to the executor."""
        self._executor = executor
        self._attr_unique_id = f"{entry_id}_healthy"

    async def async_added_to_hass(self) -> None:
        """Refresh on every executor state change."""
        self.async_on_remove(self._executor.add_listener(self._handle_executor_change))

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True while the executor considers actuation safe."""
        return self._executor.healthy

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reason behind the current state."""
        return {"status": self._executor.status}
