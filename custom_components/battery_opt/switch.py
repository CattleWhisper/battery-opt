"""
Manual-override switches: gate the actuations, never the loops.

- switch.battery_opt_executor_actuation — off: the 15-minute executor
  keeps planning, validating and guarding, but skips every driver
  write. Its commanded state is forgotten while off, so turning it
  back on replays the FULL transition sequence — safe after the owner
  has manually driven the battery in the meantime.
- switch.battery_opt_charge_loop_actuation — off: the charge-power
  loop keeps computing (its fallback flag stays live) but writes no
  setpoints.

Both default to ON and restore their last state across restarts.
They exist only when the corresponding component runs (no battery →
no switches).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import device_info_for

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BatteryOptConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA-required signature
    entry: BatteryOptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the actuation override switches (active mode only)."""
    runtime = entry.runtime_data
    entities: list[ActuationSwitch] = []
    if runtime.executor is not None:
        entities.append(
            ActuationSwitch(
                entry.entry_id,
                name="Executor actuation",
                unique_suffix="executor_actuation",
                target=runtime.executor,
            )
        )
    if runtime.charge_loop is not None:
        entities.append(
            ActuationSwitch(
                entry.entry_id,
                name="Charge loop actuation",
                unique_suffix="charge_loop_actuation",
                target=runtime.charge_loop,
            )
        )
    async_add_entities(entities)


class ActuationSwitch(SwitchEntity, RestoreEntity):
    """One actuation gate; the wrapped component keeps computing."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:hand-back-right"

    def __init__(
        self,
        entry_id: str,
        name: str,
        unique_suffix: str,
        target: Any,  # executor or charge loop: both duck-type the flag
    ) -> None:
        """Bind the switch to its component's actuation flag."""
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = device_info_for(entry_id)
        self._target = target
        self._attr_is_on = True

    def _apply(self) -> None:
        self._target.actuation_enabled = bool(self._attr_is_on)

    async def async_added_to_hass(self) -> None:
        """Restore the last state; default is ON (actuation live)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "off":
            self._attr_is_on = False
        self._apply()

    async def async_turn_on(self, **_kwargs: object) -> None:
        """Re-enable actuation (full transition replays next tick)."""
        self._attr_is_on = True
        self._apply()
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Disable actuation; the loops keep computing."""
        self._attr_is_on = False
        self._apply()
        self.async_write_ha_state()
