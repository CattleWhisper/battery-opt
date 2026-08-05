"""
Sensors for battery_opt (spec §8).

- sensor.battery_opt_plan: with a battery, the executor's last
  commanded action; in planning-only mode, the advisory plan's action
  for the current quarter-hour. The advisory plan vectors ride in the
  attributes either way.
- sensor.battery_opt_forecast_savings: the advisory capped-greedy
  plan's forecast saving vs not cycling (EUR/day, excl. fixed terms
  and VAT), from real OMIE prices.
- sensor.battery_opt_vs_static: forecast gain vs the fixed seasonal
  schedule — the metric that justifies the project (spec §8).
- sensor.battery_opt_current_price: the delivered energy price right
  now per the EDP Indexada formula (core.prices.price), EUR/kWh excl.
  fixed terms and VAT. Declared exactly like core OMIE's price sensor
  so the Energy dashboard accepts it as a grid price entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .core.calendar import period
from .entity import device_info_for

if TYPE_CHECKING:
    from datetime import datetime

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
    """Create the plan, savings and price sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            PlanSensor(runtime.coordinator, runtime.executor, entry.entry_id),
            ForecastSavingsSensor(runtime.coordinator, entry.entry_id),
            VsStaticSensor(runtime.coordinator, entry.entry_id),
            CurrentPriceSensor(runtime.coordinator, entry.entry_id),
        ]
    )


class QuarterHourMixin(SensorEntity):
    """
    Rewrite state on quarter-hour boundaries.

    The coordinator refresh is not wall-clock aligned, but prices and
    plan slots change exactly at :00/:15/:30/:45 — without this the
    state lags the boundary by up to a full update interval.
    """

    async def async_added_to_hass(self) -> None:
        """Register the quarter-hour rewrite."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._on_quarter_hour, minute=[0, 15, 30, 45], second=0
            )
        )

    @callback
    def _on_quarter_hour(self, _now: datetime) -> None:
        self.async_write_ha_state()


class PlanSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """Current action plus the advisory day plan."""

    _attr_has_entity_name = True
    _attr_name = "Plan"
    _attr_suggested_object_id = "battery_opt_plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor | None,
        entry_id: str,
    ) -> None:
        """Bind to the coordinator and, when present, the executor."""
        super().__init__(coordinator)
        self._executor = executor
        self._attr_unique_id = f"{entry_id}_plan"
        self._attr_device_info = device_info_for(entry_id)

    async def async_added_to_hass(self) -> None:
        """Also refresh whenever the executor state changes."""
        await super().async_added_to_hass()
        if self._executor is not None:
            self.async_on_remove(
                self._executor.add_listener(self._handle_executor_change)
            )

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Executor's last command, or the advisory action right now."""
        if self._executor is not None:
            return self._executor.last_action
        return self.coordinator.planned_action_now()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The advisory plan, and the executor state when it exists."""
        data = self.coordinator.data or {}
        attributes: dict[str, Any] = {
            "mode": "active" if self._executor is not None else "planning_only",
            "plan_date": str(data.get("plan_date") or ""),
            "charge_w": data.get("plan_charge_w"),
            "discharge_w": data.get("plan_discharge_w"),
            "prices_ok": data.get("prices_ok"),
            "prices_padded": data.get("prices_padded"),
        }
        if self._executor is not None:
            attributes["executor_status"] = self._executor.status
            attributes["executor_plan_date"] = str(self._executor.plan_day or "")
        return attributes


class ForecastSavingsSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Advisory plan's forecast saving vs not cycling, EUR/day."""

    _attr_has_entity_name = True
    _attr_name = "Forecast savings"
    _attr_suggested_object_id = "battery_opt_forecast_savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_forecast_savings"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """Excl. fixed terms and VAT (spec §4); None until prices exist."""
        return (self.coordinator.data or {}).get("forecast_saving_eur")


class CurrentPriceSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """Delivered energy price now, per the EDP Indexada formula."""

    _attr_has_entity_name = True
    _attr_name = "Current price"
    _attr_suggested_object_id = "battery_opt_current_price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}"
    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_current_price"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """EUR/kWh excl. fixed terms and VAT; None until prices exist."""
        return self.coordinator.current_price_eur_kwh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the TAR period and the whole day for graphing."""
        data = self.coordinator.data or {}
        prices = data.get("prices_eur_kwh")
        return {
            "tar_period": period(dt_util.now()),
            "plan_date": str(data.get("plan_date") or ""),
            "prices_eur_kwh": (
                [round(p, 5) for p in prices] if prices is not None else None
            ),
            "prices_padded": data.get("prices_padded"),
        }


class VsStaticSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Forecast gain of the capped greedy over the static schedule."""

    _attr_has_entity_name = True
    _attr_name = "Vs static"
    _attr_suggested_object_id = "battery_opt_vs_static"
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_vs_static"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """EUR/day, excl. fixed terms and VAT; None until prices exist."""
        return (self.coordinator.data or {}).get("vs_static_eur")
