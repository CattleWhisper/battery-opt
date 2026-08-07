"""
Tests for the battery driver (custom_components/battery_opt/driver.py).

ADR-0004: the integration never opens Modbus; every write goes through
hass.services.async_call against marstek_modbus entities. The hass
object is duck-typed, so these tests run without homeassistant
installed — a stub records the calls the real driver makes, and the
FakeDriver stands in for the executor tests.

ADR-0006: three battery states. The transition sequences asserted here
are the spec §8 sequences verbatim; entity surface verified against
ViperRNMC/marstek_venus_modbus registers/e_v3.yaml (force_mode options
standby/charge/discharge, user_work_mode options manual/anti_feed/
trade_mode — YAML keys exposed unmodified; rs485_control_mode is a
switch; the SOC cutoff numbers are MISSING on the V3 map).
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.battery_opt.driver import (
    DriverError,
    DriverUnavailableError,
    FakeDriver,
    MarstekDriver,
    MarstekEntities,
)

ENTITIES = MarstekEntities(
    mode_select="select.marstek_force_mode",
    charge_power_number="number.marstek_set_charge_power",
    soc_sensor="sensor.marstek_battery_soc",
    rs485_switch="switch.marstek_rs485_control_mode",
    work_mode_select="select.marstek_user_work_mode",
)

ENTITIES_FULL = MarstekEntities(
    mode_select="select.marstek_force_mode",
    charge_power_number="number.marstek_set_charge_power",
    soc_sensor="sensor.marstek_battery_soc",
    rs485_switch="switch.marstek_rs485_control_mode",
    work_mode_select="select.marstek_user_work_mode",
    charge_to_soc_number="number.marstek_charge_to_soc",
    charge_cutoff_number="number.marstek_charging_cutoff_capacity",
    discharge_cutoff_number="number.marstek_discharging_cutoff_capacity",
)


class StubServices:
    """Records service calls; can be told to fail the next N calls."""

    def __init__(self) -> None:
        """Start with an empty recording and no scheduled failures."""
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_next = 0

    async def async_call(self, domain: str, service: str, data: dict) -> None:
        """Record the call, or raise if a failure is scheduled."""
        if self.fail_next > 0:
            self.fail_next -= 1
            msg = "modbus write failed"
            raise RuntimeError(msg)
        self.calls.append((domain, service, data))


class StubStates:
    """Minimal hass.states with per-entity state strings."""

    def __init__(self, states: dict[str, str] | None = None) -> None:
        """Hold the state strings entities will report."""
        self.states = states or {}

    def get(self, entity_id: str) -> object | None:
        """Return a state-like object, or None when unavailable."""
        if entity_id not in self.states:
            return None
        return SimpleNamespace(entity_id=entity_id, state=self.states[entity_id])


def _hass(states: dict[str, str] | None = None) -> SimpleNamespace:
    if states is None:
        states = {"sensor.marstek_battery_soc": "57.0"}
    return SimpleNamespace(services=StubServices(), states=StubStates(states))


def _summary(hass: SimpleNamespace) -> list[tuple]:
    """Compress recorded calls to (service, entity, value-ish)."""
    out = []
    for domain, service, data in hass.services.calls:
        value = data.get("option", data.get("value"))
        out.append((domain, service, data["entity_id"], value))
    return out


def test_enter_charge_from_unknown_runs_the_full_sequence() -> None:
    """Spec §8 → CHARGE: rs485 on → power → backstop → force charge."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES_FULL)
    asyncio.run(driver.set_state("charge", charge_power_w=2000.0, target_soc_pct=85.0))
    assert _summary(hass) == [
        ("switch", "turn_on", "switch.marstek_rs485_control_mode", None),
        ("number", "set_value", "number.marstek_set_charge_power", 2000.0),
        ("number", "set_value", "number.marstek_charge_to_soc", 85.0),
        ("select", "select_option", "select.marstek_force_mode", "charge"),
    ]


def test_backstop_skipped_when_entity_not_configured() -> None:
    """No charge_to_soc entity → no backstop write (checklist gate)."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("charge", charge_power_w=2000.0, target_soc_pct=85.0))
    entities_written = [data["entity_id"] for _, _, data in hass.services.calls]
    assert "number.marstek_charge_to_soc" not in entities_written
    assert len(hass.services.calls) == 3


def test_backstop_clamps_to_the_upstream_number_range() -> None:
    """charge_to_soc is a 10-100 % number upstream; clamp both ends."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES_FULL)
    asyncio.run(driver.set_state("charge", charge_power_w=500.0, target_soc_pct=5.0))
    backstop = next(
        data["value"]
        for _, _, data in hass.services.calls
        if data["entity_id"] == "number.marstek_charge_to_soc"
    )
    assert backstop == 10.0


def test_charge_requires_a_power_setpoint() -> None:
    """CHARGE without charge_power_w is a caller bug."""
    driver = MarstekDriver(_hass(), ENTITIES)
    with pytest.raises(ValueError, match="charge_power_w"):
        asyncio.run(driver.set_state("charge"))


def test_enter_discharge_releases_control_and_asserts_anti_feed() -> None:
    """Spec §8 → DISCHARGE: force stop → rs485 off → anti_feed."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("discharge"))
    assert _summary(hass) == [
        ("select", "select_option", "select.marstek_force_mode", "standby"),
        ("switch", "turn_off", "switch.marstek_rs485_control_mode", None),
        ("select", "select_option", "select.marstek_user_work_mode", "anti_feed"),
    ]


def test_anti_feed_reasserted_on_every_discharge_entry() -> None:
    """Force mode flips work mode to manual — never assume it stuck."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("discharge"))
    asyncio.run(driver.set_state("charge", charge_power_w=1000.0))
    asyncio.run(driver.set_state("discharge"))
    anti_feed_writes = [
        data for _, _, data in hass.services.calls if data.get("option") == "anti_feed"
    ]
    assert len(anti_feed_writes) == 2


def test_charge_to_hold_is_a_single_write() -> None:
    """Spec §8 internal: from CHARGE, HOLD is just force standby."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("charge", charge_power_w=2000.0))
    hass.services.calls.clear()
    asyncio.run(driver.set_state("hold"))
    assert _summary(hass) == [
        ("select", "select_option", "select.marstek_force_mode", "standby"),
    ]


def test_hold_to_charge_skips_the_rs485_write() -> None:
    """Spec §8 internal: HOLD→CHARGE without re-engaging control."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("hold"))
    hass.services.calls.clear()
    asyncio.run(driver.set_state("charge", charge_power_w=1500.0))
    assert _summary(hass) == [
        ("number", "set_value", "number.marstek_set_charge_power", 1500.0),
        ("select", "select_option", "select.marstek_force_mode", "charge"),
    ]


def test_discharge_to_hold_reengages_external_control() -> None:
    """Spec §8 DISCHARGE→HOLD: rs485 on → force standby."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("discharge"))
    hass.services.calls.clear()
    asyncio.run(driver.set_state("hold"))
    assert _summary(hass) == [
        ("switch", "turn_on", "switch.marstek_rs485_control_mode", None),
        ("select", "select_option", "select.marstek_force_mode", "standby"),
    ]


def test_repeated_state_is_a_no_op() -> None:
    """Same state twice → no service calls the second time."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("hold"))
    hass.services.calls.clear()
    asyncio.run(driver.set_state("hold"))
    assert hass.services.calls == []


def test_failed_transition_replays_the_full_sequence() -> None:
    """A mid-sequence failure leaves state unknown → full replay."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_state("hold"))
    hass.services.fail_next = 1
    with pytest.raises(DriverError):
        asyncio.run(driver.set_state("charge", charge_power_w=1000.0))
    hass.services.calls.clear()
    # State is unknown now: entering HOLD must re-engage rs485 even
    # though it was engaged before the failed transition.
    asyncio.run(driver.set_state("hold"))
    assert _summary(hass) == [
        ("switch", "turn_on", "switch.marstek_rs485_control_mode", None),
        ("select", "select_option", "select.marstek_force_mode", "standby"),
    ]


def test_read_soc_parses_the_sensor_state() -> None:
    """SoC comes from the sensor entity as a percentage."""
    driver = MarstekDriver(_hass(), ENTITIES)
    assert asyncio.run(driver.read_soc()) == pytest.approx(57.0)


def test_read_soc_unavailable_raises_driver_error() -> None:
    """A missing or unavailable entity is a driver failure."""
    driver = MarstekDriver(_hass({}), ENTITIES)
    with pytest.raises(DriverError):
        asyncio.run(driver.read_soc())
    driver = MarstekDriver(
        _hass({"sensor.marstek_battery_soc": "unavailable"}), ENTITIES
    )
    with pytest.raises(DriverError):
        asyncio.run(driver.read_soc())


def test_three_consecutive_failures_raise_unavailable() -> None:
    """Failures 1-2 raise DriverError; the third DriverUnavailableError."""
    hass = _hass()
    hass.services.fail_next = 3
    driver = MarstekDriver(hass, ENTITIES)
    for _ in range(2):
        with pytest.raises(DriverError) as err:
            asyncio.run(driver.set_charge_power(500.0))
        assert not isinstance(err.value, DriverUnavailableError)
    with pytest.raises(DriverUnavailableError):
        asyncio.run(driver.set_charge_power(500.0))


def test_success_resets_the_failure_counter() -> None:
    """Two failures, one success, two failures: never unavailable."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    for fail_batch in (2, 2):
        hass.services.fail_next = fail_batch
        for _ in range(fail_batch):
            with pytest.raises(DriverError):
                asyncio.run(driver.set_charge_power(500.0))
        asyncio.run(driver.set_charge_power(500.0))  # success resets
    assert len(hass.services.calls) == 2


def test_cutoffs_written_once_with_compare_before_write() -> None:
    """Both cutoffs write when absent; equal values never rewrite."""
    hass = _hass(
        {
            "sensor.marstek_battery_soc": "57.0",
            "number.marstek_charging_cutoff_capacity": "100.0",
        }
    )
    driver = MarstekDriver(hass, ENTITIES_FULL)
    assert asyncio.run(driver.write_soc_cutoffs(27.0, 100.0)) is True
    # Ceiling already reads 100.0 → only the floor is written.
    assert _summary(hass) == [
        (
            "number",
            "set_value",
            "number.marstek_discharging_cutoff_capacity",
            27.0,
        ),
    ]


def test_cutoffs_missing_entities_are_non_fatal() -> None:
    """V3 (upstream MISSING list): unset entities → False, no calls."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    assert asyncio.run(driver.write_soc_cutoffs(27.0, 100.0)) is False
    assert hass.services.calls == []


def test_cutoff_write_rejection_is_non_fatal_and_uncounted() -> None:
    """A rejected cutoff write neither raises nor feeds the 3-strike."""
    hass = _hass()
    hass.services.fail_next = 2
    driver = MarstekDriver(hass, ENTITIES_FULL)
    assert asyncio.run(driver.write_soc_cutoffs(27.0, 100.0)) is False
    # The failure counter must be untouched: three normal calls now
    # must NOT escalate to DriverUnavailableError on the first.
    asyncio.run(driver.set_charge_power(500.0))


def test_fake_driver_records_the_call_sequence() -> None:
    """The executor tests replay against this exact recording."""

    async def scenario(driver: FakeDriver) -> float:
        await driver.set_state("charge", charge_power_w=2000.0, target_soc_pct=90.0)
        await driver.set_charge_power(1500.0)
        await driver.set_state("hold")
        return await driver.read_soc()

    fake = FakeDriver(soc_percent=27.0)
    soc = asyncio.run(scenario(fake))
    assert fake.calls == [
        ("set_state", ("charge", 2000.0, 90.0)),
        ("set_charge_power", 1500.0),
        ("set_state", ("hold", None, None)),
        ("read_soc", None),
    ]
    assert soc == pytest.approx(27.0)
