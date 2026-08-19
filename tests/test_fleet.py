"""
Tests for the ADR-0009 fleet: allocation, FleetDriver, unit resolution.

HA-free like the driver tests: fake drivers and a duck-typed config
entry, no hass. The HA-level fleet behaviour (migration, subentry
flows, aggregate params) lives in test_config_flow.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import pytest

from custom_components.battery_opt.driver import (
    BatteryDriver,
    DriverError,
    DriverUnavailableError,
    FakeDriver,
    FleetDriver,
    allocate_charge_w,
)
from custom_components.battery_opt.fleet import battery_units, fleet_power_sensors

UNIT_MAX_W = 2500.0

BATTERY_KEYS = {
    "mode_select": "select.m1_force_mode",
    "charge_power_number": "number.m1_set_charge_power",
    "rs485_switch": "switch.m1_rs485",
    "work_mode_select": "select.m1_work_mode",
}
SECOND_BATTERY_KEYS = {
    "mode_select": "select.m2_force_mode",
    "charge_power_number": "number.m2_set_charge_power",
    "rs485_switch": "switch.m2_rs485",
    "work_mode_select": "select.m2_work_mode",
}


def test_allocation_is_capacity_proportional() -> None:
    """The ADR-0009 example: 3800 W over 5 + 7 kWh, floored to 50 W."""
    shares = allocate_charge_w(3800.0, [5.0, 7.0], unit_max_w=UNIT_MAX_W)
    # 5/12 x 3800 = 1583.3 -> 1550; 7/12 x 3800 = 2216.7 -> 2200.
    assert shares == [1550.0, 2200.0]


def test_allocation_caps_at_the_unit_max_and_redistributes() -> None:
    """A share above the device max spills to the unit with headroom."""
    shares = allocate_charge_w(4800.0, [5.0, 7.0], unit_max_w=UNIT_MAX_W)
    # 7/12 x 4800 = 2800 > 2500: capped, the 300 W excess goes to the
    # 5 kWh unit (2000 + 300 = 2300, still under its max).
    assert shares == [2300.0, 2500.0]


def test_allocation_clamps_to_the_fleet_total() -> None:
    """More than N x max cannot be placed anywhere."""
    shares = allocate_charge_w(9000.0, [5.0, 7.0], unit_max_w=UNIT_MAX_W)
    assert shares == [2500.0, 2500.0]


def test_allocation_single_unit_passes_through() -> None:
    """N=1 parity: a stepped total comes back unchanged."""
    assert allocate_charge_w(2000.0, [5.0], unit_max_w=UNIT_MAX_W) == [2000.0]
    assert allocate_charge_w(3000.0, [5.0], unit_max_w=UNIT_MAX_W) == [2500.0]


def test_allocation_zero_capacities_split_evenly() -> None:
    """Degenerate zero weights fall back to equal shares, no crash."""
    assert allocate_charge_w(2000.0, [0.0, 0.0], unit_max_w=UNIT_MAX_W) == [
        1000.0,
        1000.0,
    ]


def test_allocation_empty_and_zero_total() -> None:
    """No units or no power: empty/zero shares."""
    assert allocate_charge_w(1000.0, [], unit_max_w=UNIT_MAX_W) == []
    assert allocate_charge_w(0.0, [5.0, 7.0], unit_max_w=UNIT_MAX_W) == [0.0, 0.0]


@dataclass
class _FailingDriver(BatteryDriver):
    """Driver whose every call raises the configured error."""

    error: DriverError
    calls: list[str] = field(default_factory=list)

    async def set_state(self, state: str, **_: Any) -> None:  # type: ignore[override]
        self.calls.append(f"set_state:{state}")
        raise self.error

    async def set_charge_power(self, watts: float) -> None:
        self.calls.append(f"set_charge_power:{watts}")
        raise self.error

    async def write_soc_cutoffs(self, _floor_pct: float, _ceiling_pct: float) -> bool:
        self.calls.append("write_soc_cutoffs")
        return False


async def test_fleet_broadcasts_states_and_splits_charge_power() -> None:
    """CHARGE splits proportionally; HOLD broadcasts with no power."""
    first, second = FakeDriver(), FakeDriver()
    fleet = FleetDriver([first, second], [5.0, 7.0], unit_max_w=UNIT_MAX_W)

    await fleet.set_state("charge", charge_power_w=3800.0, target_soc_pct=99.0)
    assert first.calls == [("set_state", ("charge", 1550.0, 99.0))]
    assert second.calls == [("set_state", ("charge", 2200.0, 99.0))]

    await fleet.set_state("hold")
    assert first.calls[-1] == ("set_state", ("hold", None, None))
    assert second.calls[-1] == ("set_state", ("hold", None, None))

    await fleet.set_charge_power(2400.0)
    assert first.calls[-1] == ("set_charge_power", 1000.0)
    assert second.calls[-1] == ("set_charge_power", 1400.0)


async def test_fleet_attempts_every_unit_then_raises_the_worst() -> None:
    """One unavailable unit fails the fleet, AFTER the others act."""
    healthy = FakeDriver()
    broken = _FailingDriver(DriverUnavailableError("3 consecutive"))
    fleet = FleetDriver([broken, healthy], [5.0, 5.0], unit_max_w=UNIT_MAX_W)

    with pytest.raises(DriverUnavailableError):
        await fleet.set_state("hold")
    # The healthy unit was still commanded — a transient failure on
    # one unit must not leave the other stuck in the previous state.
    assert healthy.calls == [("set_state", ("hold", None, None))]

    flaky = _FailingDriver(DriverError("transient"))
    fleet = FleetDriver([flaky, healthy], [5.0, 5.0], unit_max_w=UNIT_MAX_W)
    with pytest.raises(DriverError) as excinfo:
        await fleet.set_charge_power(2000.0)
    assert not isinstance(excinfo.value, DriverUnavailableError)


async def test_fleet_cutoffs_are_anded() -> None:
    """write_soc_cutoffs is True only when every unit confirms."""
    fleet = FleetDriver([FakeDriver(), FakeDriver()], [5.0, 5.0], unit_max_w=UNIT_MAX_W)
    assert await fleet.write_soc_cutoffs(27.0, 100.0) is True
    mixed = FleetDriver(
        [FakeDriver(), _FailingDriver(DriverError("x"))],
        [5.0, 5.0],
        unit_max_w=UNIT_MAX_W,
    )
    assert await mixed.write_soc_cutoffs(27.0, 100.0) is False


def test_fleet_requires_matching_units_and_caps() -> None:
    """A capacity per unit, at least one unit."""
    with pytest.raises(ValueError, match="one capacity per unit"):
        FleetDriver([FakeDriver()], [5.0, 7.0], unit_max_w=UNIT_MAX_W)
    with pytest.raises(ValueError, match="at least one unit"):
        FleetDriver([], [], unit_max_w=UNIT_MAX_W)


@dataclass(frozen=True)
class _FakeSubentry:
    data: MappingProxyType
    subentry_type: str
    subentry_id: str


@dataclass
class _FakeEntry:
    data: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    subentries: dict[str, _FakeSubentry] = field(default_factory=dict)


def _subentry(subentry_id: str, data: dict[str, Any]) -> _FakeSubentry:
    return _FakeSubentry(
        data=MappingProxyType(data), subentry_type="battery", subentry_id=subentry_id
    )


def test_battery_units_prefers_subentries_in_id_order() -> None:
    """Subentries win outright over stale parent keys; order is stable."""
    entry = _FakeEntry(
        data={**BATTERY_KEYS, "capacity_kwh": 99.0},  # stale, must be ignored
        subentries={
            "b": _subentry("b", {**SECOND_BATTERY_KEYS, "capacity_kwh": 7.0}),
            "a": _subentry(
                "a",
                {
                    **BATTERY_KEYS,
                    "capacity_kwh": 5.0,
                    "self_discharge_w": 25.0,
                    "battery_power_sensor": "sensor.m1_power",
                },
            ),
        },
    )
    units = battery_units(entry)
    assert [unit.capacity_kwh for unit in units] == [5.0, 7.0]
    assert units[0].self_discharge_w == 25.0
    assert units[1].self_discharge_w == 19.0  # default
    assert units[0].power_sensor == "sensor.m1_power"
    assert units[0].entities.mode_select == BATTERY_KEYS["mode_select"]


def test_battery_units_legacy_flat_group_is_one_unit() -> None:
    """Pre-migration entries: the parent flat keys define one unit."""
    entry = _FakeEntry(data={**BATTERY_KEYS, "capacity_kwh": 5.0})
    units = battery_units(entry)
    assert len(units) == 1
    assert units[0].capacity_kwh == 5.0
    assert battery_units(_FakeEntry()) == []


def test_fleet_power_sensors_all_or_nothing() -> None:
    """A partial sensor set is worthless: one unmetered unit poisons the sum."""
    both = _FakeEntry(
        subentries={
            "a": _subentry(
                "a", {**BATTERY_KEYS, "battery_power_sensor": "sensor.m1_power"}
            ),
            "b": _subentry(
                "b",
                {**SECOND_BATTERY_KEYS, "battery_power_sensor": "sensor.m2_power"},
            ),
        }
    )
    assert fleet_power_sensors(both) == ["sensor.m1_power", "sensor.m2_power"]
    partial = _FakeEntry(
        subentries={
            "a": _subentry(
                "a", {**BATTERY_KEYS, "battery_power_sensor": "sensor.m1_power"}
            ),
            "b": _subentry("b", dict(SECOND_BATTERY_KEYS)),
        }
    )
    assert fleet_power_sensors(partial) == []
    # No units at all: the parent-entry sensor stands (meter configured
    # ahead of the battery).
    orphan = _FakeEntry(options={"battery_power_sensor": "sensor.house_batt"})
    assert fleet_power_sensors(orphan) == ["sensor.house_batt"]
