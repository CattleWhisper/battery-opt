"""
Tests for the 15-minute executor (custom_components/battery_opt/executor.py).

Pure-logic tests: the executor module is HA-free (the timer wiring
lives in __init__), so these drive tick() directly with the
FakeDriver and assert the exact call sequences the driver tests
promised. Spec §9/§11: validate C-1..C-7 before every actuation,
never actuate when unhealthy, three driver failures set healthy off
with a notification.

ADR-0006: intervals map to CHARGE/HOLD/DISCHARGE states; set_state is
issued only on decision changes and DISCHARGE never carries a power
setpoint. The executor reads no SoC (owner decision 2026-08-07): the
reserve floor is the battery's to manage.
"""

import asyncio
from datetime import date, datetime

import pytest

from custom_components.battery_opt.core.plan import BatteryParams, Plan
from custom_components.battery_opt.driver import (
    DriverError,
    DriverUnavailableError,
    FakeDriver,
)
from custom_components.battery_opt.executor import BatteryOptExecutor, DynamicDayPlan

PARAMS = BatteryParams()


def _dynamic_payload(day: date) -> DynamicDayPlan:
    """
    Build a valid greedy-like plan that visibly differs from the static.

    Charges 00:00-01:00, discharges 13:00-13:30 — winter 13:00 is HOLD
    on the static schedule, so a discharge command proves adoption.
    """
    n = 96
    charge = [0.0] * n
    discharge = [0.0] * n
    for i in range(4):
        charge[i] = 2000.0
    discharge[52] = 500.0
    discharge[53] = 500.0
    return DynamicDayPlan(
        day=day,
        plan=Plan(charge_w=tuple(charge), discharge_w=tuple(discharge)),
        params=BatteryParams(),
        load_w=(1040.0,) * n,
        solar_w=(0.0,) * n,
    )


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
    plan_factory: object = None,
    notifications: list[str] | None = None,
) -> BatteryOptExecutor:
    return BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        plan_factory=plan_factory,
        notify=(notifications.append if notifications is not None else None),
    )


def test_charge_interval_transitions_with_power_and_backstop() -> None:
    """
    Winter weekday 00:00: the vazio charge window opens.

    One set_state carries the ENTRY setpoint (ADR-0007: from the
    charge-power loop's fallback here — never from the plan) and the
    charge-to-SoC backstop — the static plan fills to 100 %.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert driver.calls == [("set_state", ("charge", 2000.0, 100.0))]
    assert executor.healthy is True
    assert executor.last_action == "charge"


def test_charge_entry_uses_the_loop_setpoint_and_reports_it() -> None:
    """ADR-0007: entry power comes from the loop; the loop is told."""
    written: list[float] = []
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_charge_entry_w=lambda: 2500.0,
        on_charge_entry=written.append,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert driver.calls == [("set_state", ("charge", 2500.0, 100.0))]
    assert written == [2500.0]


def test_discharge_interval_is_a_mode_switch_never_a_setpoint() -> None:
    """
    Ponta: DISCHARGE delegates magnitude to the firmware's anti-feed.

    ADR-0006: force-discharge exports when house load drops below the
    setpoint, so the executor must never send a discharge power. The
    floor-seeded plan charged 00:00-07:00, so 10:00 ponta discharges.
    """
    driver = FakeDriver()
    executor = _executor(driver)
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


def test_executor_never_touches_power_within_charge() -> None:
    """
    ADR-0007: the charge-power loop owns the setpoint inside CHARGE.

    Even when the plan's internal power values differ per quarter,
    consecutive CHARGE ticks issue no driver calls at all.
    """

    def factory(_day: object, load: list, _solar: list, _params: object) -> Plan:
        n = len(load)
        charge = [0.0] * n
        charge[0] = 2000.0
        charge[1] = 1500.0  # a state selector only, never a setpoint
        return Plan(charge_w=tuple(charge), discharge_w=(0.0,) * n)

    driver = FakeDriver()
    executor = _executor(driver, plan_factory=factory)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 15)))
    assert len(driver.calls) == 1
    assert driver.calls[0][0] == "set_state"


def test_actuation_disabled_computes_everything_but_writes_nothing() -> None:
    """
    Manual override: the tick runs fully, only driver writes skip.

    last_action still reflects the decision (the plan sensor keeps
    showing what the plan wants), health stays managed, and status
    carries the disabled marker.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    executor.actuation_enabled = False
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert driver.calls == []
    assert executor.healthy is True
    assert executor.last_action == "charge"
    assert "actuation disabled" in executor.status


def test_reenabling_actuation_replays_the_full_transition() -> None:
    """
    After a manual period the battery state is unknown: full replay.

    Even if the decision never changed while disabled, the first
    enabled tick must issue set_state again.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))  # hold, commanded
    executor.actuation_enabled = False
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))  # manual period
    executor.actuation_enabled = True
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 30)))  # same decision
    assert driver.calls == [
        ("set_state", ("hold", None, None)),
        ("set_state", ("hold", None, None)),
    ]
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


def test_plan_rebuilds_on_date_change_seeded_at_the_floor() -> None:
    """
    Midnight rollover: a fresh plan starts at the reserve floor.

    No live SoC seed (owner decision 2026-08-07): the plan is a
    schedule, and both run-time magnitudes are closed loops.
    """
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
        plan_factory=spy_factory,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 23, 45)))
    asyncio.run(executor.tick(datetime(2026, 1, 16, 0, 0)))
    assert [day for day, _ in seen] == [
        datetime(2026, 1, 15).date(),
        datetime(2026, 1, 16).date(),
    ]
    assert all(soc == pytest.approx(PARAMS.cap_min_kwh) for _, soc in seen)


def test_current_action_reflects_the_active_interval() -> None:
    """The plan sensor's state source follows the tick."""
    driver = FakeDriver()
    executor = _executor(driver)
    assert executor.current_action(datetime(2026, 1, 15, 0, 0)) == "unknown"
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    assert executor.current_action(datetime(2026, 1, 15, 0, 0)) == "charge"
    assert executor.current_action(datetime(2026, 1, 15, 13, 0)) == "hold"


def test_summer_ponta_discharges_via_day_chaining() -> None:
    """
    Summer Monday 10:00 ponta commands DISCHARGE.

    The floor-seeded single-day plan used to sit in HOLD through every
    summer ponta (charge window after it, nothing above the floor to
    serve it) — with virtual day-chaining, Friday's midday charge
    carries over the weekend and the morning ponta actually runs.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 8, 10, 10, 0)))
    assert driver.calls == [("set_state", ("discharge", None, None))]
    assert executor.healthy is True
    assert executor.last_action == "discharge"


def test_day_chaining_soc_trajectory_starts_at_the_chained_seed() -> None:
    """The published trajectory uses the same seeded start as the plan."""
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 8, 10, 10, 0)))
    trajectory = executor.planned_soc_trajectory()
    assert trajectory is not None
    assert trajectory[0] == pytest.approx(PARAMS.cap_usable_kwh)  # full


def test_charge_state_holds_through_the_armed_window_tail() -> None:
    """
    Winter 05:00 — hours past model-full — still commands CHARGE.

    Owner decision 2026-08-13: the window stays armed at a marginal
    state-selector power, so the run-time pair (charge loop at full
    power + the firmware percent-target) owns the stop. Real
    shortfalls (deeper-than-forecast discharge, a throttled loop)
    recover in the remaining window time.
    """
    driver = FakeDriver()
    executor = _executor(driver)
    asyncio.run(executor.tick(datetime(2026, 1, 15, 0, 0)))
    asyncio.run(executor.tick(datetime(2026, 1, 15, 5, 0)))
    assert executor.last_action == "charge"
    assert len(driver.calls) == 1  # unchanged state: no rewrite


def test_dynamic_static_fallback_seeds_at_the_floor() -> None:
    """
    Dry-run off: the fallback must not model the static chain's seed.

    Under dynamic actuation yesterday's greedy ended the day at the
    floor, so a summer morning ponta genuinely has nothing to serve it
    — the fallback plan holds instead of claiming a discharge the
    battery cannot deliver.
    """
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        dynamic_enabled=True,
    )
    asyncio.run(executor.tick(datetime(2026, 8, 10, 10, 0)))  # summer ponta
    assert executor.plan_source == "static-fallback"
    assert executor.last_action == "hold"
    trajectory = executor.planned_soc_trajectory()
    assert trajectory is not None
    assert trajectory[0] == pytest.approx(PARAMS.cap_min_kwh)


def test_dynamic_plan_adopted_and_actuated() -> None:
    """
    Task 12: with dynamic enabled, the executor actuates the greedy.

    Winter 13:00 is HOLD on the static schedule; the dynamic payload
    discharges there, so a discharge command proves adoption.
    """
    day = date(2026, 1, 15)
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_dynamic_plan=lambda: _dynamic_payload(day),
        dynamic_enabled=True,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert executor.plan_source == "greedy"
    assert driver.calls == [("set_state", ("discharge", None, None))]
    assert executor.healthy is True


def test_dry_run_ignores_the_dynamic_plan() -> None:
    """Task 12 dry-run (the default): greedy advisory, static actuates."""
    day = date(2026, 1, 15)
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_dynamic_plan=lambda: _dynamic_payload(day),
        # dynamic_enabled stays at its default: False (dry-run).
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert executor.plan_source == "static"
    assert driver.calls == [("set_state", ("hold", None, None))]


def test_dynamic_missing_falls_back_then_upgrades_same_day() -> None:
    """
    No greedy yet: the chained static actuates, marked as fallback.

    The first tick after the coordinator publishes the greedy adopts
    it — e.g. the 00:00 tick precedes the 00:00:30 refresh.
    """
    holder: list[DynamicDayPlan | None] = [None]
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        get_dynamic_plan=lambda: holder[0],
        dynamic_enabled=True,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert executor.plan_source == "static-fallback"
    assert executor.last_action == "hold"  # winter 13:00 static
    holder[0] = _dynamic_payload(date(2026, 1, 15))
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert executor.plan_source == "greedy"
    assert executor.last_action == "discharge"  # 13:15 in the payload


def test_invalid_dynamic_plan_demotes_to_static() -> None:
    """
    Task 12 fail-closed (decision 6): an invalid greedy never actuates.

    The executor demotes to the chained static, stays healthy, notifies
    once, and never re-adopts the same rejected object.
    """
    n = 96
    bad = DynamicDayPlan(
        day=date(2026, 1, 15),
        plan=Plan(charge_w=(0.0,) * n, discharge_w=(5000.0,) * n),  # C-1
        params=BatteryParams(),
        load_w=(1040.0,) * n,
        solar_w=(0.0,) * n,
    )
    notifications: list[str] = []
    driver = FakeDriver()
    executor = BatteryOptExecutor(
        driver=driver,
        get_params=lambda: PARAMS,
        notify=notifications.append,
        get_dynamic_plan=lambda: bad,
        dynamic_enabled=True,
    )
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 0)))
    assert executor.healthy is True
    assert executor.plan_source == "static-fallback"
    assert executor.last_action == "hold"  # the static schedule's 13:00
    assert len(notifications) == 1
    assert "dynamic plan invalid" in notifications[0]
    # The same rejected object is not re-adopted: no notification spam,
    # and the static keeps actuating.
    asyncio.run(executor.tick(datetime(2026, 1, 15, 13, 15)))
    assert executor.plan_source == "static-fallback"
    assert len(notifications) == 1
