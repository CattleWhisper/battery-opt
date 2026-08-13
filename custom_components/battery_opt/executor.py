"""
15-minute executor: applies the current interval's battery state.

Phase 1 runs the static seasonal plan. Task 12 (shipped 2026-08-13,
dry-run default ON): with `dynamic_enabled`, the executor instead
adopts the coordinator's validated capped-greedy plan for the day
(`DynamicDayPlan` — the plan together with the params and load/solar
vectors it was built with, so re-validation cannot produce false
violations), falling back to the chained static whenever no dynamic
plan exists yet; a fallback day upgrades to the greedy on the first
tick after the coordinator publishes it. An invalid dynamic plan is
demoted to static, never actuated (decision 6 fail-closed). Every
tick re-validates the whole plan against C-1..C-7 before touching the
battery (spec §11) — an invalid plan means no actuation at all.

ADR-0006 state machine, ADR-0007 states-only plan: each interval maps
to CHARGE, HOLD or DISCHARGE — the plan's power vectors are pure
state selectors. Both run-time magnitudes are closed loops: discharge
is the firmware's anti-feed, charge power is owned by the
charge-power loop (charge_loop.py) from CHARGE entry to exit; the
executor only supplies the entry setpoint the loop hands it.
Transition sequences live in the driver; the executor issues
`set_state` only on decision changes (spec §8: write once, rewrite on
change).

The reserve floor is the battery's to manage (owner decision
2026-08-07): the plan honours C-4 in its model, but at run time the
floor is enforced by the firmware discharge cutoff where the register
exists — on the Venus E V3 (register MISSING upstream) by the
device's own internal minimum. The executor reads no SoC and runs no
floor guard.

Health policy (spec §9): the driver's third consecutive failure
(DriverUnavailableError) flips `healthy` off and notifies once; a
later fully-successful tick restores it. Transient failures below the
limit keep `healthy` on — the next tick retries, and the commanded
state is forgotten so the full transition replays. The plan rebuilds
at date change, seeded at the previous weekday's PLANNED end SoC
(virtual day-chaining, `core.static_schedule.chained_start_soc`) —
without it the summer schedule could never discharge, since its
charge window sits after the morning ponta. The plan stays a
schedule, not a tracker: the seed is the model rolled forward, and
no SoC is ever read.

This module is HA-free like the driver: the quarter-hour timer and
the notification service live in the integration __init__; tests
drive tick() directly.
"""

from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING, Literal

from .charge_loop import CHARGE_FALLBACK_W
from .const import BASE_LOAD_W
from .core.plan import BatteryParams, Plan, soc_trajectory, validate_plan
from .core.static_schedule import chained_start_soc, static_plan
from .driver import DriverError, DriverUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from .driver import BatteryDriver, BatteryState

    PlanFactory = Callable[
        [date, Sequence[float], Sequence[float], BatteryParams], Plan
    ]

INTERVALS_PER_DAY = 96
INTERVAL_MINUTES = 15

# charge_to_soc backstop margin: slightly above the planned end-of-
# window SoC so the firmware stop never truncates the plan itself.
BACKSTOP_MARGIN_PCT = 2.0

Action = Literal["charge", "discharge", "hold", "unknown"]

# What the executor is actuating: the fixed seasonal schedule
# (dry-run / Phase 1), the coordinator's greedy (Task 12 live), or
# static because no trustworthy greedy exists for today.
PlanSource = Literal["static", "greedy", "static-fallback"]


@dataclasses.dataclass(frozen=True)
class DynamicDayPlan:
    """
    A day's validated greedy plan with the inputs it was built with.

    The coordinator publishes this (Task 12); the executor validates
    against the SAME params and load/solar vectors — validating a
    greedy plan against the executor's own flat load would produce
    false C-1 violations wherever the forecast exceeds it.
    """

    day: date
    plan: Plan
    params: BatteryParams
    load_w: tuple[float, ...]
    solar_w: tuple[float, ...]


class BatteryOptExecutor:
    """Applies the day's plan interval by interval through the driver."""

    # The two ADR-0007 hooks push the count past the limit; grouping
    # them into a config object would obscure a simple wiring surface.
    def __init__(  # noqa: PLR0913
        self,
        driver: BatteryDriver,
        get_params: Callable[[], BatteryParams],
        plan_factory: PlanFactory | None = None,
        notify: Callable[[str], None] | None = None,
        get_charge_entry_w: Callable[[], float] | None = None,
        on_charge_entry: Callable[[float], None] | None = None,
        get_dynamic_plan: Callable[[], DynamicDayPlan | None] | None = None,
        *,
        dynamic_enabled: bool = False,
    ) -> None:
        """
        Wire the driver and the coordinator-backed data sources.

        ADR-0007: the plan carries states only. `get_charge_entry_w`
        supplies the setpoint written on CHARGE entry — the charge-power
        loop's current value when the loop is wired, the conservative
        static fallback otherwise; `on_charge_entry` tells the loop what
        was written so its deadband baseline matches reality.

        Task 12: `dynamic_enabled` (config `dry_run` inverted; dry-run
        is the default) makes the executor actuate the coordinator's
        greedy plan from `get_dynamic_plan`, with static as fallback.
        """
        self._driver = driver
        self._get_params = get_params
        self._plan_factory: PlanFactory = plan_factory or static_plan
        self._notify = notify or (lambda _message: None)
        self._get_charge_entry_w = get_charge_entry_w or (lambda: CHARGE_FALLBACK_W)
        self._on_charge_entry = on_charge_entry or (lambda _watts: None)
        self._get_dynamic_plan = get_dynamic_plan or (lambda: None)
        self.dynamic_enabled = dynamic_enabled
        self._listeners: list[Callable[[], None]] = []
        self.healthy = False
        self.status = "no tick yet"
        self.last_action: Action = "unknown"
        self.plan: Plan | None = None
        self.plan_day: date | None = None
        self.plan_source: PlanSource = "static"
        self._plan_params: BatteryParams | None = None
        self._load_w = [BASE_LOAD_W] * INTERVALS_PER_DAY
        self._solar_w = [0.0] * INTERVALS_PER_DAY
        # The vectors the CURRENT plan was built with — validation must
        # use these, not self._load_w (a dynamic plan carries its own).
        self._plan_load_w: list[float] = list(self._load_w)
        self._plan_solar_w: list[float] = list(self._solar_w)
        # A dynamic plan demoted by tick-time validation; the identity
        # check stops re-adopting the same object every tick while the
        # coordinator's next refresh (a new object) retries cleanly.
        self._rejected_dynamic: Plan | None = None
        self._demote_notified_day: date | None = None
        # Last state the driver confirmed; None forces a full apply.
        self._commanded_state: BatteryState | None = None
        # Manual override (switch.battery_opt_executor_actuation):
        # False = keep computing everything, skip the driver writes.
        self.actuation_enabled = True

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change callback; returns the remover."""
        self._listeners.append(listener)

        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    def _set_health(self, *, healthy: bool, status: str) -> None:
        # Notify on the transition into unhealthy — including a failure
        # on the very first tick ("no tick yet" counts as pre-healthy).
        # Repeats of the same failure state stay silent.
        went_unhealthy = not healthy and (self.healthy or self.status == "no tick yet")
        changed = (healthy, status) != (self.healthy, self.status)
        self.healthy = healthy
        self.status = status
        if went_unhealthy:
            self._notify(f"battery_opt unhealthy: {status}")
        if changed:
            for listener in self._listeners:
                listener()

    def _ensure_plan(self, now: datetime) -> tuple[Plan, BatteryParams]:
        today = now.date()
        cached = (
            self.plan is not None
            and self._plan_params is not None
            and self.plan_day == today
        )
        if cached and (not self.dynamic_enabled or self.plan_source == "greedy"):
            return self.plan, self._plan_params
        if self.dynamic_enabled:
            dynamic = self._get_dynamic_plan()
            if (
                dynamic is not None
                and dynamic.day == today
                and dynamic.plan is not self._rejected_dynamic
            ):
                self.plan = dynamic.plan
                self.plan_day = today
                self._plan_params = dynamic.params
                self._plan_load_w = list(dynamic.load_w)
                self._plan_solar_w = list(dynamic.solar_w)
                self.plan_source = "greedy"
                return dynamic.plan, dynamic.params
            if cached:
                # Keep today's static fallback; the greedy is adopted
                # on the first tick after the coordinator publishes it
                # (e.g. the 00:00 tick precedes the 00:00:30 refresh).
                return self.plan, self._plan_params
        return self._build_static(today)

    def _build_static(self, today: date) -> tuple[Plan, BatteryParams]:
        # Virtual day-chaining: seed today from the previous weekday's
        # PLANNED end SoC — the summer schedule can only discharge in
        # its morning ponta if yesterday's midday charge carries over
        # (in winter the previous day ends drained, so the seed IS the
        # floor). Still a schedule, not a tracker: this rolls the
        # plan's own model forward, no SoC readback (ADR-0008). The
        # seeded params are kept so validation and the published SoC
        # trajectory use the same start the plan was built with.
        base = self._get_params()
        params = dataclasses.replace(
            base,
            soc_start_kwh=chained_start_soc(today, self._load_w, self._solar_w, base),
        )
        self.plan = self._plan_factory(today, self._load_w, self._solar_w, params)
        self.plan_day = today
        self._plan_params = params
        self._plan_load_w = list(self._load_w)
        self._plan_solar_w = list(self._solar_w)
        self.plan_source = "static-fallback" if self.dynamic_enabled else "static"
        return self.plan, params

    def planned_soc_trajectory(self) -> list[float] | None:
        """Planned SoC in kWh at interval boundaries (97 values)."""
        if self.plan is None or self._plan_params is None:
            return None
        return soc_trajectory(self.plan, self._plan_params)

    def current_action(self, now: datetime) -> Action:
        """Return what the plan does in the interval containing `now`."""
        if self.plan is None or self.plan_day != now.date():
            return "unknown"
        index = self._interval_index(now)
        if index >= len(self.plan):
            return "unknown"
        # ADR-0007: the plan's power vectors are pure state selectors.
        if self.plan.charge_w[index] > 0:
            return "charge"
        if self.plan.discharge_w[index] > 0:
            return "discharge"
        return "hold"

    @staticmethod
    def _interval_index(now: datetime) -> int:
        return (now.hour * 60 + now.minute) // INTERVAL_MINUTES

    def _charge_window_target_pct(
        self, plan: Plan, params: BatteryParams, index: int
    ) -> float:
        """Planned end-of-window SoC as a firmware backstop target."""
        end = index
        while end < len(plan) and plan.charge_w[end] > 0:
            end += 1
        soc_end_kwh = soc_trajectory(plan, params)[end]
        pct = math.ceil(soc_end_kwh / params.cap_usable_kwh * 100.0)
        return min(100.0, pct + BACKSTOP_MARGIN_PCT)

    async def tick(self, now: datetime) -> None:
        """Validate, then apply the current interval's battery state."""
        plan, plan_params = self._ensure_plan(now)
        violations = validate_plan(
            plan, self._plan_load_w, self._plan_solar_w, plan_params
        )
        if violations and self.plan_source == "greedy":
            # Task 12 fail-closed (decision 6): an invalid dynamic plan
            # is demoted to the chained static, never actuated. Cannot
            # happen by construction — the coordinator only publishes
            # validated plans — so it is belt-and-braces; notify at
            # most once a day to avoid a chatty pathological loop.
            self._rejected_dynamic = plan
            if self._demote_notified_day != now.date():
                self._demote_notified_day = now.date()
                self._notify(
                    f"battery_opt: dynamic plan invalid ({violations[0]}); "
                    "falling back to the static schedule"
                )
            plan, plan_params = self._build_static(now.date())
            violations = validate_plan(
                plan, self._plan_load_w, self._plan_solar_w, plan_params
            )
        if violations:
            self._set_health(healthy=False, status=f"invalid plan: {violations[0]}")
            return
        index = self._interval_index(now)
        desired: Action = (
            "charge"
            if plan.charge_w[index] > 0
            else "discharge"
            if plan.discharge_w[index] > 0
            else "hold"
        )
        if not self.actuation_enabled:
            # Manual override: everything above still ran (plan and
            # validation) — only the driver write is skipped. The
            # commanded state is forgotten so re-enabling replays the
            # FULL transition: the user may have changed anything.
            self._commanded_state = None
            self.last_action = desired
            self._set_health(healthy=True, status="ok (actuation disabled)")
            return
        try:
            if desired != self._commanded_state:
                # ADR-0007: entry power comes from the charge-power
                # loop (or its fallback), never from the plan; within
                # CHARGE the loop owns every subsequent setpoint.
                entry_w = self._get_charge_entry_w() if desired == "charge" else None
                target = (
                    self._charge_window_target_pct(plan, plan_params, index)
                    if desired == "charge"
                    else None
                )
                await self._driver.set_state(
                    desired,
                    charge_power_w=entry_w,
                    target_soc_pct=target,
                )
                self._commanded_state = desired
                if entry_w is not None:
                    self._on_charge_entry(entry_w)
        except DriverUnavailableError as err:
            self._commanded_state = None  # unknown: replay next tick
            self._set_health(healthy=False, status=f"driver unavailable: {err}")
            return
        except DriverError as err:
            # Below the three-strike limit: retry next tick, stay as-is.
            self._commanded_state = None
            self.status = f"transient driver error: {err}"
            return
        self.last_action = desired
        self._set_health(healthy=True, status="ok")
