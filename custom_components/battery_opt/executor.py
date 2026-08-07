"""
15-minute executor: applies the current interval's battery state.

Phase 1 runs the static seasonal plan; Task 12 swaps in the greedy
with static as fallback. Every tick re-validates the whole plan
against C-1..C-7 before touching the battery (spec §11) — an invalid
plan or a missing SoC reading means no actuation at all.

ADR-0006 state machine, ADR-0007 states-only plan: each interval maps
to CHARGE, HOLD or DISCHARGE — the plan's power vectors are pure
state selectors. Both run-time magnitudes are closed loops: discharge
is the firmware's anti-feed, charge power is owned by the
charge-power loop (charge_loop.py) from CHARGE entry to exit; the
executor only supplies the entry setpoint the loop hands it.
Transition sequences live in the driver; the executor issues
`set_state` only on decision changes (spec §8: write once, rewrite on
change).

The reserve floor is never delegated (spec §8): during DISCHARGE,
SoC at or below the floor forces HOLD, and DISCHARGE is only allowed
again once SoC recovers above floor + hysteresis — the firmware
cutoff registers are MISSING on the Venus E V3, so this guard is the
primary floor protection, not belt-and-braces.

Health policy (spec §9): the driver's third consecutive failure
(DriverUnavailableError) flips `healthy` off and notifies once; a
later fully-successful tick restores it. Transient failures below the
limit keep `healthy` on — the next tick retries, and the commanded
state is forgotten so the full transition replays. The plan rebuilds
at date change, seeded with the measured SoC so days chain like the
backtest.

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
from .core.static_schedule import static_plan
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

# Floor-guard hysteresis (spec §8): once the guard forces HOLD,
# DISCHARGE needs SoC ≥ floor + this margin — no flapping at the edge.
FLOOR_GUARD_HYSTERESIS_KWH = 0.15

# charge_to_soc backstop margin: slightly above the planned end-of-
# window SoC so the firmware stop never truncates the plan itself.
BACKSTOP_MARGIN_PCT = 2.0

Action = Literal["charge", "discharge", "hold", "unknown"]


class BatteryOptExecutor:
    """Applies the day's plan interval by interval through the driver."""

    # The two ADR-0007 hooks push the count past the limit; grouping
    # them into a config object would obscure a simple wiring surface.
    def __init__(  # noqa: PLR0913
        self,
        driver: BatteryDriver,
        get_params: Callable[[], BatteryParams],
        get_soc_kwh: Callable[[], float | None],
        plan_factory: PlanFactory | None = None,
        notify: Callable[[str], None] | None = None,
        get_charge_entry_w: Callable[[], float] | None = None,
        on_charge_entry: Callable[[float], None] | None = None,
    ) -> None:
        """
        Wire the driver and the coordinator-backed data sources.

        ADR-0007: the plan carries states only. `get_charge_entry_w`
        supplies the setpoint written on CHARGE entry — the charge-power
        loop's current value when the loop is wired, the conservative
        static fallback otherwise; `on_charge_entry` tells the loop what
        was written so its deadband baseline matches reality.
        """
        self._driver = driver
        self._get_params = get_params
        self._get_soc_kwh = get_soc_kwh
        self._plan_factory: PlanFactory = plan_factory or static_plan
        self._notify = notify or (lambda _message: None)
        self._get_charge_entry_w = get_charge_entry_w or (lambda: CHARGE_FALLBACK_W)
        self._on_charge_entry = on_charge_entry or (lambda _watts: None)
        self._listeners: list[Callable[[], None]] = []
        self.healthy = False
        self.status = "no tick yet"
        self.last_action: Action = "unknown"
        self.plan: Plan | None = None
        self.plan_day: date | None = None
        self._plan_params: BatteryParams | None = None
        self._load_w = [BASE_LOAD_W] * INTERVALS_PER_DAY
        self._solar_w = [0.0] * INTERVALS_PER_DAY
        # Last state the driver confirmed; None forces a full apply.
        self._commanded_state: BatteryState | None = None
        self._floor_guard_active = False
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

    def _ensure_plan(self, now: datetime, soc_kwh: float) -> tuple[Plan, BatteryParams]:
        if (
            self.plan is not None
            and self._plan_params is not None
            and self.plan_day == now.date()
        ):
            return self.plan, self._plan_params
        params = dataclasses.replace(self._get_params(), soc_start_kwh=soc_kwh)
        self.plan = self._plan_factory(now.date(), self._load_w, self._solar_w, params)
        self.plan_day = now.date()
        self._plan_params = params
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

    def _apply_floor_guard(
        self, desired: Action, soc_kwh: float, params: BatteryParams
    ) -> Action:
        """Never delegate the reserve floor (spec §8), with hysteresis."""
        if desired != "discharge":
            return desired
        if self._floor_guard_active:
            if soc_kwh >= params.cap_min_kwh + FLOOR_GUARD_HYSTERESIS_KWH:
                self._floor_guard_active = False
                return "discharge"
            return "hold"
        if soc_kwh <= params.cap_min_kwh:
            self._floor_guard_active = True
            return "hold"
        return "discharge"

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
        soc_kwh = self._get_soc_kwh()
        if soc_kwh is None:
            self._set_health(healthy=False, status="battery SoC unavailable")
            return
        plan, plan_params = self._ensure_plan(now, soc_kwh)
        violations = validate_plan(plan, self._load_w, self._solar_w, plan_params)
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
        guarded = self._apply_floor_guard(desired, soc_kwh, plan_params)
        if not self.actuation_enabled:
            # Manual override: everything above still ran (plan,
            # validation, guards) — only the driver write is skipped.
            # The commanded state is forgotten so re-enabling replays
            # the FULL transition: the user may have changed anything.
            self._commanded_state = None
            self.last_action = guarded
            suffix = "" if guarded == desired else "; floor guard: holding"
            self._set_health(healthy=True, status=f"ok (actuation disabled{suffix})")
            return
        try:
            if guarded != self._commanded_state:
                # ADR-0007: entry power comes from the charge-power
                # loop (or its fallback), never from the plan; within
                # CHARGE the loop owns every subsequent setpoint.
                entry_w = self._get_charge_entry_w() if guarded == "charge" else None
                target = (
                    self._charge_window_target_pct(plan, plan_params, index)
                    if guarded == "charge"
                    else None
                )
                await self._driver.set_state(
                    guarded,
                    charge_power_w=entry_w,
                    target_soc_pct=target,
                )
                self._commanded_state = guarded
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
        self.last_action = guarded
        status = "ok" if guarded == desired else "ok (floor guard: holding)"
        self._set_health(healthy=True, status=status)
