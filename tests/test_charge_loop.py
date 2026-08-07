"""
Tests for the charge-power control loop (ADR-0007, Task 15).

Pure-logic tests: charge_loop.py is HA-free; time is injected and the
FakeDriver records writes. The control law keeps measured total grid
import under the contracted ceiling with the 200 W margin, clamps to
the device's 2500 W, floors to 50 W steps, and falls back to the
proven static 2000 W when either sensor drops out.
"""

import asyncio

import pytest

from custom_components.battery_opt.charge_loop import (
    ChargePowerLoop,
    charge_setpoint_w,
)
from custom_components.battery_opt.driver import DriverError, FakeDriver


def test_control_law_uses_full_device_power_when_room_allows() -> None:
    """Flat 1040 W house load: 4400-1040=3360 clamps to the 2500 max."""
    assert charge_setpoint_w(1040.0, 0.0) == 2500.0


def test_control_law_throttles_on_load_spike() -> None:
    """Battery at 2500, kettle pushes import to 4600: other load 2100."""
    assert charge_setpoint_w(4600.0, 2500.0) == 2300.0


def test_control_law_floors_to_the_register_step() -> None:
    """4400-2130=2270 floors DOWN to 2250 — never up (C-3 margin)."""
    assert charge_setpoint_w(2130.0, 0.0) == 2250.0


def test_control_law_clamps_at_zero() -> None:
    """House load alone above the ceiling: no room to charge at all."""
    assert charge_setpoint_w(4800.0, 0.0) == 0.0


def _loop(
    driver: FakeDriver,
    inputs: dict,
    charging: dict,
) -> ChargePowerLoop:
    return ChargePowerLoop(
        driver,
        get_inputs=lambda: inputs["value"],
        is_charging=lambda: charging["value"],
    )


def test_loop_writes_the_clamped_setpoint_while_charging() -> None:
    """A meter update during CHARGE lands one clamped write."""
    driver = FakeDriver()
    loop = _loop(driver, {"value": (1040.0, 0.0)}, {"value": True})
    asyncio.run(loop.on_update(now=100.0))
    assert driver.calls == [("set_charge_power", 2500.0)]
    assert loop.fallback is False


def test_loop_is_inert_outside_charge() -> None:
    """Not in CHARGE: no writes, baseline reset for the next entry."""
    driver = FakeDriver()
    loop = _loop(driver, {"value": (1040.0, 0.0)}, {"value": False})
    asyncio.run(loop.on_update(now=100.0))
    assert driver.calls == []


def test_deadband_swallows_meter_noise() -> None:
    """A 90 W wiggle (< 100 W deadband) never reaches the register."""
    driver = FakeDriver()
    inputs = {"value": (1040.0, 0.0)}
    loop = _loop(driver, inputs, {"value": True})
    asyncio.run(loop.on_update(now=100.0))
    inputs["value"] = (2000.0, 0.0)  # 4400-2000=2400: 100 below, on the edge
    asyncio.run(loop.on_update(now=200.0))
    inputs["value"] = (1990.0, 0.0)  # 2400 -> 2400 (floored): inside band
    asyncio.run(loop.on_update(now=300.0))
    assert driver.calls == [
        ("set_charge_power", 2500.0),
        ("set_charge_power", 2400.0),
    ]


def test_rate_limit_defers_rapid_changes() -> None:
    """A big change within 5 s of the last write waits for the next event."""
    driver = FakeDriver()
    inputs = {"value": (1040.0, 0.0)}
    loop = _loop(driver, inputs, {"value": True})
    asyncio.run(loop.on_update(now=100.0))
    inputs["value"] = (
        4000.0,
        2500.0,
    )  # spike: target drops to 2900->2500... 4400-1500=2900 clamp 2500
    inputs["value"] = (4600.0, 2500.0)  # other load 2100 -> target 2300
    asyncio.run(loop.on_update(now=102.0))  # 2 s later: deferred
    assert len(driver.calls) == 1
    asyncio.run(loop.on_update(now=106.0))  # next event after the window
    assert driver.calls[-1] == ("set_charge_power", 2300.0)


def test_sensor_loss_falls_back_and_recovers() -> None:
    """Unavailable inputs -> flagged 2000 W fallback; recovery resumes."""
    driver = FakeDriver()
    inputs = {"value": (None, 0.0)}
    loop = _loop(driver, inputs, {"value": True})
    asyncio.run(loop.on_update(now=100.0))
    assert driver.calls == [("set_charge_power", 2000.0)]
    assert loop.fallback is True
    inputs["value"] = (1040.0, 2000.0)
    asyncio.run(loop.on_update(now=200.0))
    assert driver.calls[-1] == ("set_charge_power", 2500.0)
    assert loop.fallback is False


def test_entry_setpoint_feeds_the_charge_transition() -> None:
    """Executor entry uses the computed value; mark_written sets the baseline."""
    driver = FakeDriver()
    inputs = {"value": (1040.0, 0.0)}
    loop = _loop(driver, inputs, {"value": True})
    assert loop.entry_setpoint_w() == 2500.0
    loop.mark_written(2500.0, now=100.0)
    asyncio.run(loop.on_update(now=200.0))
    assert driver.calls == []  # already at the value the entry wrote


def test_entry_setpoint_falls_back_without_sensors() -> None:
    """No sensors yet: the entry uses the conservative static value."""
    driver = FakeDriver()
    loop = _loop(driver, {"value": (None, None)}, {"value": True})
    assert loop.entry_setpoint_w() == 2000.0
    assert loop.fallback is True


def test_write_failure_is_swallowed_and_retried() -> None:
    """A DriverError neither escapes nor poisons the baseline."""

    class FailingDriver(FakeDriver):
        fail = True

        async def set_charge_power(self, watts: float) -> None:
            if self.fail:
                msg = "injected"
                raise DriverError(msg)
            await super().set_charge_power(watts)

    driver = FailingDriver()
    inputs = {"value": (1040.0, 0.0)}
    loop = _loop(driver, inputs, {"value": True})
    asyncio.run(loop.on_update(now=100.0))  # swallowed
    driver.fail = False
    asyncio.run(loop.on_update(now=101.0))  # retried immediately: no
    # baseline was recorded, so neither deadband nor rate limit applies
    assert driver.calls == [("set_charge_power", 2500.0)]


def test_import_spike_mid_window_never_breaches_the_ceiling() -> None:
    """Acceptance: total import stays under 4400 across a spike cycle."""
    driver = FakeDriver()
    inputs = {"value": (3540.0, 2500.0)}  # house 1040 + battery 2500
    loop = _loop(driver, inputs, {"value": True})
    asyncio.run(loop.on_update(now=100.0))
    # AC compressor kicks in: house jumps to 2600 W. Import spikes.
    inputs["value"] = (5100.0, 2500.0)
    asyncio.run(loop.on_update(now=110.0))
    new_setpoint = driver.calls[-1][1]
    house_load = 5100.0 - 2500.0
    assert new_setpoint + house_load <= 4400.0
    assert new_setpoint == pytest.approx(1800.0)
