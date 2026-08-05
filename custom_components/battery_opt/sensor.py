"""
Sensors for battery_opt (spec §8).

- sensor.battery_opt_plan: the executor's last commanded action, with
  the full day plan in attributes.
- sensor.battery_opt_forecast_savings: placeholder until price
  ingestion (Task 10) and the dynamic plan (Task 12) can compute it —
  unknown, never a made-up number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    """Create the plan and forecast-savings sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            PlanSensor(runtime.coordinator, runtime.executor, entry.entry_id),
            ForecastSavingsSensor(runtime.coordinator, entry.entry_id),
        ]
    )


class PlanSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """The executor's last commanded action plus the day plan."""

    _attr_name = "Battery Opt Plan"
    _attr_suggested_object_id = "battery_opt_plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor,
        entry_id: str,
    ) -> None:
        """Bind to the coordinator and the executor."""
        super().__init__(coordinator)
        self._executor = executor
        self._attr_unique_id = f"{entry_id}_plan"

    async def async_added_to_hass(self) -> None:
        """Also refresh whenever the executor state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self._executor.add_listener(self._handle_executor_change))

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """The last action the executor commanded."""
        return self._executor.last_action

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the whole plan for dashboards and debugging."""
        plan = self._executor.plan
        return {
            "plan_date": str(self._executor.plan_day) if plan else None,
            "charge_w": list(plan.charge_w) if plan else None,
            "discharge_w": list(plan.discharge_w) if plan else None,
            "status": self._executor.status,
        }


class ForecastSavingsSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Forecast saving vs not cycling — populated from Task 12 on."""

    _attr_name = "Battery Opt Forecast Savings"
    _attr_suggested_object_id = "battery_opt_forecast_savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_forecast_savings"

    @property
    def native_value(self) -> float | None:
        """Unknown until prices exist (Task 10/12) — never invented."""
        return None
