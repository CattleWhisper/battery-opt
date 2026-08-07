"""
Health binary sensor (spec §8): on = safe to actuate.

Off on an invalid plan or a three-strike driver failure — exactly the
executor's health latch. The executor never actuates while this is
off (spec §11); the sensor only reports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity import device_info_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BatteryOptConfigEntry
    from .coordinator import BatteryOptCoordinator
    from .executor import BatteryOptExecutor


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA-required signature
    entry: BatteryOptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the health sensor."""
    runtime = entry.runtime_data
    async_add_entities(
        [HealthySensor(runtime.coordinator, runtime.executor, entry.entry_id)]
    )


class HealthySensor(CoordinatorEntity["BatteryOptCoordinator"], BinarySensorEntity):
    """The executor's latch — or, planning-only, price/plan health."""

    _attr_has_entity_name = True
    _attr_name = "Healthy"
    _attr_suggested_object_id = "battery_opt_healthy"
    _attr_icon = "mdi:heart-pulse"

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor | None,
        entry_id: str,
    ) -> None:
        """Bind to the coordinator and, when present, the executor."""
        super().__init__(coordinator)
        self._executor = executor
        self._attr_unique_id = f"{entry_id}_healthy"
        self._attr_device_info = device_info_for(entry_id)

    async def async_added_to_hass(self) -> None:
        """Refresh on every executor state change."""
        await super().async_added_to_hass()
        if self._executor is not None:
            self.async_on_remove(
                self._executor.add_listener(self._handle_executor_change)
            )

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Safe to actuate — or, planning-only, plans are computable."""
        if self._executor is not None:
            return self._executor.healthy
        return bool(
            self.coordinator.last_update_success
            and (self.coordinator.data or {}).get("prices_ok")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reason behind the current state."""
        if self._executor is not None:
            return {"status": self._executor.status}
        prices_ok = (self.coordinator.data or {}).get("prices_ok")
        status = "planning only: ok" if prices_ok else "planning only: no prices"
        return {"status": status}
