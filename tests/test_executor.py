"""
Tests for the 15-minute executor (custom_components/battery_opt/executor.py).

Pure-logic tests: the executor module is HA-free (the timer wiring
lives in __init__), so these drive tick() directly with the
FakeDriver from Task 7 and assert the exact call sequences the
Task 7 tests promised. Spec §9/§11: validate C-1..C-7 before every
actuation, never actuate when unhealthy, three driver failures set
healthy off with a notification.
"""

import asyncio
from datetime import datetime

import pytest

from custom_components.battery_opt.core.plan import BatteryParams, Plan
from custom_components.battery_opt.driver import (
    DriverError,
    DriverUnavailableError,
    FakeDriver,
)
from custom_components.battery_opt.executor import BatteryOptExecutor

PARAMS = BatteryParams()


class FlakyDriver(FakeDriver):
    """FakeDriver that can be told to fail mode changes."""

    fail_with: type[Exception] | None = None

    async def set_mode(self, mode: str) -> None:
        """Raise the configured error, or record the call."""
        if self.fail_with is not None:
            msg = "injected failure"
            raise self.fail_with(msg)
        await super().set_mode(mode)


def _executor(
    driver: FakeDriver,
    soc_kwh: float | None = 1.35,
    plan_factory: object = None,
    notifications: list[str] | None = None,
) -> BatteryOptExecutor:
    return BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_soc_kwh=lambda: soc_kwh,
        plan_factory=plan_factory,
        notify=(notifications.append if notifications is not None else None),
    )


def test_charge_interval_sets_power_then_mode() -> None:
    """
    Winter weekday 00:00: the vazio charge window starts at 2000 W.

    (The static plan charges to full in ~2 h from midnight, so later
    vazio intervals like 03:00 are correctly idle.)
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert driver.calls == [("set_charge_power", 2000.0), ("set_mode", "charge")]
    assert executor.healthy is True


def test_ponta_interval_discharges_floored_to_device_step() -> None:
    """
    Ponta at 1040 W net load: setpoint floors to 1000 W (50 W step).

    Rounding is DOWN, never up — rounding a discharge up would export
    (C-1) and rounding a charge up could breach the contracted-power
    margin (C-3).
    """
    driver = FakeDriver()
    executor = _executor(driver, soc_kwh=5.0)  # full: ponta can be served
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 0)))
    assert driver.calls == [
        ("set_discharge_power", 1000.0),
        ("set_mode", "discharge"),
    ]


def test_idle_interval_commands_standby_only() -> None:
    """Winter 13:00 is cheias with no window: idle, nothing else."""
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert driver.calls == [("set_mode", "idle")]


def test_invalid_plan_never_actuates() -> None:
    """A plan violating C-1..C-7 blocks all actuation (spec §11)."""

    def broken_factory(
        _day: object, _load: object, _solar: object, _params: object
    ) -> Plan:
        return Plan(charge_w=(0.0,) * 96, discharge_w=(5000.0,) * 96)

    driver = FakeDriver()
    notifications: list[str] = []
    executor = _executor(
        driver, plan_factory=broken_factory, notifications=notifications
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 3, 0)))
    assert driver.calls == []
    assert executor.healthy is False
    assert "invalid plan" in executor.status
    assert notifications  # the human is told


def test_missing_soc_blocks_actuation() -> None:
    """No SoC reading: unhealthy, no driver calls."""
    driver = FakeDriver()
    executor = _executor(driver, soc_kwh=None)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 3, 0)))
    assert driver.calls == []
    assert executor.healthy is False


def test_driver_unavailable_goes_unhealthy_then_recovers() -> None:
    """
    Three-strike failure: healthy off, notified once, then recovery.

    A later fully-successful tick restores healthy.
    """
    driver = FlakyDriver()
    notifications: list[str] = []
    executor = _executor(driver, notifications=notifications)
    driver.fail_with = DriverUnavailableError
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert executor.healthy is False
    assert len(notifications) == 1  # first-ever failure notifies too
    driver.fail_with = None
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert executor.healthy is True
    # No duplicate notification on a second failure after recovery.
    driver.fail_with = DriverUnavailableError
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 30)))
    assert len(notifications) == 2


def test_transient_driver_error_stays_healthy() -> None:
    """One or two failures (below the driver's limit) do not flip health."""
    driver = FlakyDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))  # healthy baseline
    driver.fail_with = DriverError
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert executor.healthy is True  # retry next tick; the driver counts


def test_plan_rebuilds_on_date_change_seeded_with_current_soc() -> None:
    """Midnight rollover: a fresh plan starts from the measured SoC."""
    seen: list[tuple[object, float]] = []

    def spy_factory(
        day: object, load: list, _solar: list, params: BatteryParams
    ) -> Plan:
        seen.append((day, params.start_soc_kwh))
        n = len(load)
        return Plan(charge_w=(0.0,) * n, discharge_w=(0.0,) * n)

    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_soc_kwh=lambda: 3.2,
        plan_factory=spy_factory,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 23, 45)))
    asyncio.run(executor.tick(datetime(2026, 1, 16, 0, 0)))
    assert [day for day, _ in seen] == [
        datetime(2026, 1, 15).date(),
        datetime(2026, 1, 16).date(),
    ]
    assert all(soc == pytest.approx(3.2) for _, soc in seen)


def test_current_action_reflects_the_active_interval() -> None:
    """The plan sensor's state source follows the tick."""
    driver = FakeDriver()
    executor = _executor(driver)
    assert executor.current_action(datetime(2026, 1, 15, 0, 0)) == "unknown"
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert executor.current_action(datetime(2026, 1, 15, 0, 0)) == "charge"
    assert executor.current_action(datetime(2026, 1, 15, 13, 0)) == "idle"
