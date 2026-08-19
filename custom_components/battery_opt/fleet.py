"""
Resolve the battery fleet from a config entry (ADR-0009, Task 16).

Subentries of type `battery` define the fleet, in subentry-id order
(stable across restarts — allocation shares must be deterministic).
With no battery subentries, the legacy flat entity group on the parent
entry defines a single unit (pre-migration entries, and every test
that builds one); with neither, the entry is planning-only.

The config entry is duck-typed (`subentries`, `data`, `options`) so
this module needs no homeassistant import and resolution is testable
HA-free, like the driver it feeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import (
    BATTERY_ENTITY_KEYS,
    CONF_BATTERY_POWER_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_CUTOFF_NUMBER,
    CONF_CHARGE_POWER_NUMBER,
    CONF_CHARGE_TO_SOC_NUMBER,
    CONF_DISCHARGE_CUTOFF_NUMBER,
    CONF_MODE_SELECT,
    CONF_RS485_SWITCH,
    CONF_SELF_DISCHARGE_W,
    CONF_WORK_MODE_SELECT,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_SELF_DISCHARGE_W,
    SUBENTRY_TYPE_BATTERY,
)
from .driver import MarstekEntities

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class BatteryUnit:
    """One battery: its control entities and physical parameters."""

    entities: MarstekEntities
    capacity_kwh: float
    self_discharge_w: float
    power_sensor: str | None


def _unit_from(data: Mapping[str, Any]) -> BatteryUnit:
    return BatteryUnit(
        entities=MarstekEntities(
            mode_select=data[CONF_MODE_SELECT],
            charge_power_number=data[CONF_CHARGE_POWER_NUMBER],
            rs485_switch=data[CONF_RS485_SWITCH],
            work_mode_select=data[CONF_WORK_MODE_SELECT],
            charge_to_soc_number=data.get(CONF_CHARGE_TO_SOC_NUMBER),
            charge_cutoff_number=data.get(CONF_CHARGE_CUTOFF_NUMBER),
            discharge_cutoff_number=data.get(CONF_DISCHARGE_CUTOFF_NUMBER),
        ),
        capacity_kwh=float(data.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)),
        self_discharge_w=float(
            data.get(CONF_SELF_DISCHARGE_W, DEFAULT_SELF_DISCHARGE_W)
        ),
        power_sensor=data.get(CONF_BATTERY_POWER_SENSOR) or None,
    )


def battery_units(entry: Any) -> list[BatteryUnit]:
    """
    Return the fleet: subentries first, the legacy flat group second.

    Subentries WIN outright — once a `battery` subentry exists, any
    battery keys still sitting on the parent entry (pre-migration
    leftovers, stale options) are ignored, never merged.
    """
    subentries = sorted(
        (
            subentry
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_BATTERY
        ),
        key=lambda subentry: subentry.subentry_id,
    )
    units = [
        _unit_from(subentry.data)
        for subentry in subentries
        if all(subentry.data.get(key) for key in BATTERY_ENTITY_KEYS)
    ]
    if units:
        return units
    merged = {**entry.data, **entry.options}
    if all(merged.get(key) for key in BATTERY_ENTITY_KEYS):
        return [_unit_from(merged)]
    return []


def fleet_power_sensors(entry: Any) -> list[str]:
    """
    Return the battery power sensors the fleet contributes.

    Every unit must contribute one for the list to be trusted — the
    charge loop's `other_load` and the realised tracker both need the
    COMPLETE fleet draw, and a partial sum silently miscounts a unit
    as house load. Falls back to the parent-entry sensor when there
    are no units at all (a meter configured ahead of the battery).
    """
    units = battery_units(entry)
    if units:
        sensors = [unit.power_sensor for unit in units]
        return [s for s in sensors if s] if all(sensors) else []
    merged = {**entry.data, **entry.options}
    sensor = merged.get(CONF_BATTERY_POWER_SENSOR)
    return [sensor] if sensor else []
