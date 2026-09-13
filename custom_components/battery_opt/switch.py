"""
Owner-facing switches: two actuation gates and the UPS-mode selector.

- switch.battery_opt_executor_actuation — off: the 15-minute executor
  keeps planning, validating and guarding, but skips every driver
  write. Its commanded state is forgotten while off, so turning it
  back on replays the FULL transition sequence — safe after the owner
  has manually driven the battery in the meantime.
- switch.battery_opt_charge_loop_actuation — off: the charge-power
  loop keeps computing (its fallback flag stays live) but writes no
  setpoints.
- switch.battery_opt_ups_mode — on: ADR-0011. The battery stops
  arbitraging and becomes a reserve; it still charges on the day's
  cheapest energy but never discharges.

The two actuation gates default to ON, the UPS switch to OFF; all
three restore their last state across restarts. The gates exist only
when the component they gate runs (no battery → no gates), while UPS
mode exists always: it changes the PLAN, which planning-only mode
computes too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity
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
    """Create the UPS switch, plus the actuation gates when they apply."""
    runtime = entry.runtime_data
    entities: list[SwitchEntity] = [
        UpsModeSwitch(entry.entry_id, runtime.coordinator, runtime.executor)
    ]
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


class UpsModeSwitch(SwitchEntity, RestoreEntity):
    """
    ADR-0011: hold the pack as backup instead of arbitraging it.

    Flipping it acts immediately rather than at the next quarter
    boundary — a storm warning is the reason this switch exists, and
    "the battery will stop discharging within 15 minutes" is the wrong
    answer to it. Turning it ON re-solves the day and applies the
    result now; turning it OFF does the same in reverse.

    Both halves of the system read `coordinator.ups_mode`, so there is
    one flag and it cannot be half-applied: the executor asks for it
    live on every tick, and a tick that lands before the coordinator
    has republished simply builds the offline UPS plan itself.
    """

    _attr_has_entity_name = True
    _attr_name = "UPS mode"
    _attr_icon = "mdi:power-plug-battery"

    def __init__(
        self,
        entry_id: str,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor | None,
    ) -> None:
        """Bind to the coordinator that owns the flag."""
        self._attr_unique_id = f"{entry_id}_ups_mode"
        self._attr_device_info = device_info_for(entry_id)
        self._coordinator = coordinator
        self._executor = executor
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore the last state; default is OFF (normal arbitrage)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state != "on":
            return
        self._attr_is_on = True
        # Set the flag before awaiting anything: the executor reads it
        # live, so from here on no tick can discharge the reserve, even
        # though the published plan is still the arbitrage one built by
        # the entry's first refresh.
        self._coordinator.ups_mode = True
        await self._coordinator.async_request_refresh()

    async def async_turn_on(self, **_kwargs: object) -> None:
        """Hold the pack as backup: stop discharging, keep charging."""
        await self._apply(enabled=True)

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Resume normal arbitrage planning."""
        await self._apply(enabled=False)

    async def _apply(self, *, enabled: bool) -> None:
        self._attr_is_on = enabled
        self._coordinator.ups_mode = enabled
        self.async_write_ha_state()
        await self._coordinator.async_refresh()
        if self._executor is not None:
            await self._executor.tick(dt_util.now())
