"""
Tests for the battery driver (custom_components/battery_opt/driver.py).

ADR-0004: the integration never opens Modbus; every write goes through
hass.services.async_call against marstek_modbus entities. The hass
object is duck-typed, so these tests run without homeassistant
installed — a stub records the calls the real driver makes, and the
FakeDriver stands in for the executor tests of Task 9.

Entity surface verified against ViperRNMC/marstek_venus_modbus
(registers/e_v3.yaml): mode is the `force_mode` select with options
"standby"/"charge"/"discharge" (the YAML keys verbatim — select.py
exposes them unmodified), and power is TWO number entities,
`set_charge_power` and `set_discharge_power` (0-2500 W).
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
    discharge_power_number="number.marstek_set_discharge_power",
    soc_sensor="sensor.marstek_battery_soc",
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
    """Minimal hass.states with a settable SoC entity."""

    def __init__(self, soc_state: str | None = "57.0") -> None:
        """Hold the state string the SoC sensor will report."""
        self.soc_state = soc_state

    def get(self, entity_id: str) -> object | None:
        """Return a state-like object, or None when unavailable."""
        if self.soc_state is None:
            return None
        return SimpleNamespace(entity_id=entity_id, state=self.soc_state)


def _hass(
    soc_state: str | None = "57.0",
) -> SimpleNamespace:
    return SimpleNamespace(services=StubServices(), states=StubStates(soc_state))


def test_set_charge_power_targets_the_charge_entity() -> None:
    """Charge setpoints go to set_charge_power via number.set_value."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_charge_power(500.0))
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.marstek_set_charge_power", "value": 500.0},
        )
    ]


def test_set_discharge_power_targets_the_discharge_entity() -> None:
    """Discharge setpoints go to set_discharge_power."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_discharge_power(1040.0))
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.marstek_set_discharge_power", "value": 1040.0},
        )
    ]


def test_set_mode_uses_the_force_mode_option_labels() -> None:
    """Options are the e_v3.yaml keys: charge/discharge/standby."""
    hass = _hass()
    driver = MarstekDriver(hass, ENTITIES)
    asyncio.run(driver.set_mode("charge"))
    asyncio.run(driver.set_mode("discharge"))
    asyncio.run(driver.set_mode("idle"))
    options = [data["option"] for _, _, data in hass.services.calls]
    assert options == ["charge", "discharge", "standby"]
    assert all(
        (domain, service) == ("select", "select_option")
        and data["entity_id"] == "select.marstek_force_mode"
        for domain, service, data in hass.services.calls
    )


def test_read_soc_parses_the_sensor_state() -> None:
    """SoC comes from the sensor entity as a percentage."""
    driver = MarstekDriver(_hass("57.0"), ENTITIES)
    assert asyncio.run(driver.read_soc()) == pytest.approx(57.0)


def test_read_soc_unavailable_raises_driver_error() -> None:
    """A missing or unavailable entity is a driver failure."""
    driver = MarstekDriver(_hass(None), ENTITIES)
    with pytest.raises(DriverError):
        asyncio.run(driver.read_soc())
    driver = MarstekDriver(_hass("unavailable"), ENTITIES)
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


def test_fake_driver_records_the_call_sequence() -> None:
    """Task 9's executor tests replay against this exact recording."""

    async def scenario(driver: FakeDriver) -> float:
        await driver.set_mode("charge")
        await driver.set_charge_power(2000.0)
        await driver.set_mode("idle")
        return await driver.read_soc()

    fake = FakeDriver(soc_percent=27.0)
    soc = asyncio.run(scenario(fake))
    assert fake.calls == [
        ("set_mode", "charge"),
        ("set_charge_power", 2000.0),
        ("set_mode", "idle"),
        ("read_soc", None),
    ]
    assert soc == pytest.approx(27.0)
