"""
Tests for the 15-minute executor (custom_components/battery_opt/executor.py).

Pure-logic tests: the executor module is HA-free (the timer wiring
lives in __init__), so these drive tick() directly with the
FakeDriver and assert the exact call sequences the driver tests
promised. Spec §9/§11: validate C-1..C-7 before every actuation,
never actuate when unhealthy, three driver failures set healthy off
with a notification.

ADR-0006: intervals map to CHARGE/HOLD/DISCHARGE states; set_state is
issued only on decision changes, DISCHARGE never carries a power
setpoint, and the reserve floor guard (with hysteresis) overrides
DISCHARGE because the firmware cutoffs are MISSING on the V3.
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
    """FakeDriver that can be told to fail state changes."""

    fail_with: type[Exception] | None = None

    async def set_state(
        self,
        state: str,
        *,
        charge_power_w: float | None = None,
        target_soc_pct: float | None = None,
    ) -> None:
        """Raise the configured error, or record the call."""
        if self.fail_with is not None:
            msg = "injected failure"
            raise self.fail_with(msg)
        await super().set_state(
            state, charge_power_w=charge_power_w, target_soc_pct=target_soc_pct
        )


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


def test_charge_interval_transitions_with_power_and_backstop() -> None:
    """
    Winter weekday 00:00: the vazio charge window starts at 2000 W.

    One set_state carries the setpoint and the charge-to-SoC backstop
    target — the static plan fills to 100 %, so the target is 100.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert driver.calls == [("set_state", ("charge", 2000.0, 100.0))]
    assert executor.healthy is True
    assert executor.last_action == "charge"


def test_discharge_interval_is_a_mode_switch_never_a_setpoint() -> None:
    """
    Ponta: DISCHARGE delegates magnitude to the firmware's anti-feed.

    ADR-0006: force-discharge exports when house load drops below the
    setpoint, so the executor must never send a discharge power.
    """
    driver = FakeDriver()
    executor = _executor(driver, soc_kwh=5.0)  # full: ponta can be served
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 0)))
    assert driver.calls == [("set_state", ("discharge", None, None))]
    assert executor.last_action == "discharge"


def test_hold_interval_commands_hold_only() -> None:
    """Winter 13:00 is cheias with no window: HOLD, nothing else."""
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert driver.calls == [("set_state", ("hold", None, None))]
    assert executor.last_action == "hold"


def test_unchanged_state_issues_no_calls() -> None:
    """Spec §8: write once, rewrite only on decision changes."""
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert len(driver.calls) == 1


def test_setpoint_change_within_charge_is_a_power_update_only() -> None:
    """Staying in CHARGE with a new power → set_charge_power alone."""

    def factory(_day: object, load: list, _solar: list, _params: object) -> Plan:
        n = len(load)
        charge = [0.0] * n
        charge[0] = 2000.0
        charge[1] = 1500.0
        return Plan(charge_w=tuple(charge), discharge_w=(0.0,) * n)

    driver = FakeDriver()
    executor = _executor(driver, plan_factory=factory)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 15)))
    assert driver.calls[0][0] == "set_state"
    assert driver.calls[1] == ("set_charge_power", 1500.0)


def test_floor_guard_forces_hold_with_hysteresis() -> None:
    """
    Floor during DISCHARGE → HOLD; recovery needs floor + 0.15 kWh.

    The plan validates against its build-time SoC (4.0 kWh); the guard
    watches the LIVE SoC, which is exactly how reality diverges from
    the plan when anti-feed drains faster than the planned load.
    """

    def factory(_day: object, load: list, _solar: list, _params: object) -> Plan:
        n = len(load)
        discharge = [0.0] * n
        for i in range(40, 48):  # 10:00-12:00
            discharge[i] = 1000.0
        return Plan(charge_w=(0.0,) * n, discharge_w=tuple(discharge))

    soc = {"kwh": 4.0}
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_soc_kwh=lambda: soc["kwh"],
        plan_factory=factory,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 0)))
    assert driver.calls[-1] == ("set_state", ("discharge", None, None))

    soc["kwh"] = 1.35  # at the floor: guard trips
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 15)))
    assert driver.calls[-1] == ("set_state", ("hold", None, None))
    assert executor.last_action == "hold"
    assert "floor guard" in executor.status

    soc["kwh"] = 1.45  # inside the hysteresis band: still holding
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 30)))
    assert driver.calls[-1] == ("set_state", ("hold", None, None))
    assert len(driver.calls) == 2  # no rewrite: state unchanged

    soc["kwh"] = 1.55  # recovered past floor + 0.15: discharge again
    asyncio.run(executor.tick(datetime(2026, 1, 15, 10, 45)))
    assert driver.calls[-1] == ("set_state", ("discharge", None, None))
    assert executor.status == "ok"


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

    A later fully-successful tick restores healthy — and replays the
    full transition, because the commanded state became unknown.
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
    assert driver.calls[-1] == ("set_state", ("hold", None, None))
    # A second failure after recovery notifies again — forced through
    # a state CHANGE (midnight rollover into the charge window),
    # because an unchanged state makes no driver call to fail.
    driver.fail_with = DriverUnavailableError
    asyncio.run(executor.tick(datetime(2026, 1, 16, 0, 0)))
    assert len(notifications) == 2


def test_transient_driver_error_stays_healthy_and_replays() -> None:
    """One or two failures (below the driver's limit) do not flip health."""
    driver = FlakyDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))  # healthy baseline
    driver.fail_with = DriverError
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert executor.healthy is True  # retry next tick; the driver counts
    driver.fail_with = None
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 30)))
    # Commanded state was forgotten on the failure → full replay.
    assert driver.calls[-1] == ("set_state", ("hold", None, None))


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
    assert executor.current_action(datetime(2026, 1, 15, 13, 0)) == "hold"
