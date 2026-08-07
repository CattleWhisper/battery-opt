"""
Manual action buttons: recalculate the plan, apply it now.

- `button.battery_opt_recalculate_plan` forces an immediate
  coordinator refresh — refetch prices from core OMIE, rebuild the
  load forecast, re-solve today's plan and tomorrow's preview —
  instead of waiting for the 15-minute poll or the scheduled fetch
  triggers (spec §9). A press calls the direct `async_refresh()`, not
  the debounced `async_request_refresh()`, so it acts now. Available
  in both modes — recalculating is useful in planning-only too.
- `button.battery_opt_apply_plan` (active mode only) runs a real
  executor tick immediately, applying the current quarter's state
  without waiting for the boundary. It is exactly the scheduled tick
  — plan validation, the actuation-override gate and the health latch
  all run — and the executor's write-once tracking makes it
  idempotent: an already-commanded state issues no writes. Typical
  use: after re-enabling `switch.battery_opt_executor_actuation`, put
  the battery back under plan control now instead of within 15 min.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.util import dt as dt_util

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
    """Create the action buttons (apply only exists in active mode)."""
    runtime = entry.runtime_data
    entities: list[ButtonEntity] = [
        RecalculateButton(entry.entry_id, runtime.coordinator)
    ]
    if runtime.executor is not None:
        entities.append(ApplyPlanButton(entry.entry_id, runtime.executor))
    async_add_entities(entities)


class RecalculateButton(ButtonEntity):
    """Force a full plan recomputation on demand."""

    _attr_has_entity_name = True
    _attr_name = "Recalculate plan"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry_id: str, coordinator: BatteryOptCoordinator) -> None:
        """Bind the button to the entry's coordinator."""
        self._attr_unique_id = f"{entry_id}_recalculate_plan"
        self._attr_device_info = device_info_for(entry_id)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        """Refetch prices and re-solve the plan immediately."""
        await self._coordinator.async_refresh()


class ApplyPlanButton(ButtonEntity):
    """Apply the current quarter's planned state to the battery now."""

    _attr_has_entity_name = True
    _attr_name = "Apply plan"
    _attr_icon = "mdi:play"

    def __init__(self, entry_id: str, executor: BatteryOptExecutor) -> None:
        """Bind the button to the entry's executor."""
        self._attr_unique_id = f"{entry_id}_apply_plan"
        self._attr_device_info = device_info_for(entry_id)
        self._executor = executor

    async def async_press(self) -> None:
        """Run an executor tick now instead of at the quarter boundary."""
        await self._executor.tick(dt_util.now())
