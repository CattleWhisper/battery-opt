"""
Charge-power control loop (ADR-0007, Task 15).

The plan carries states only; while the state machine is in CHARGE
this loop owns the `set_charge_power` setpoint, continuously driving
it to the highest value that keeps measured total grid import under
the contracted-power ceiling:

    other_load = measured_grid_import_w - battery_charge_w
    setpoint   = clamp(P_USABLE_W - other_load, 0, 2500)   # 50 W floor

Invariant #2 enforced against MEASUREMENT — the charge-side symmetric
of delegating zero-export to the firmware's anti-feed (ADR-0006).

HA-free like the executor and driver: the meter-event wiring lives in
the integration __init__, time is injected (monotonic seconds), and
tests drive `on_update()` directly. Failure policy: setpoint writes
share the driver's three-strike counter (a failing Modbus is a real
health signal), but the loop swallows the exceptions — the executor's
own next tick is where unavailability latches health off.

Fail safe (spec §8): either sensor unavailable → fall back to the
conservative static 2000 W (the value proven safe under the old
static-margin regime), flagged via `fallback`; sensor recovery resumes
the loop automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .driver import DriverError

if TYPE_CHECKING:
    from collections.abc import Callable

    from .driver import BatteryDriver

P_DEVICE_MAX_W = 2500.0  # Venus E 3.0 charge limit (owner, 2026-08-07)
P_USABLE_W = 4400.0  # contracted 4.6 kVA minus the standing 200 W margin
CHARGE_FALLBACK_W = 2000.0  # proven-safe static value when sensors drop out
DEADBAND_W = 100.0  # meter noise must not chatter the register
MIN_WRITE_INTERVAL_S = 5.0
_POWER_STEP_W = 50.0  # marstek_modbus number entities step in 50 W


def charge_setpoint_w(
    grid_import_w: float,
    battery_charge_w: float,
    *,
    p_usable_w: float = P_USABLE_W,
    p_max_w: float = P_DEVICE_MAX_W,
) -> float:
    """
    Compute the highest charge power under the contracted ceiling.

    Floored DOWN to the 50 W register step — rounding up could breach
    the margin (C-3).
    """
    other_load = grid_import_w - battery_charge_w
    target = min(p_max_w, max(0.0, p_usable_w - other_load))
    return int(target // _POWER_STEP_W) * _POWER_STEP_W


class ChargePowerLoop:
    """Drives the charge setpoint from grid-import measurements."""

    def __init__(
        self,
        driver: BatteryDriver,
        get_inputs: Callable[[], tuple[float | None, float | None]],
        is_charging: Callable[[], bool],
    ) -> None:
        """
        Wire the driver and the measurement/state sources.

        `get_inputs` returns (grid_import_w, battery_charge_w), either
        None when its sensor is unavailable. `is_charging` reports
        whether the state machine is currently in CHARGE.
        """
        self._driver = driver
        self._get_inputs = get_inputs
        self._is_charging = is_charging
        self._last_written_w: float | None = None
        self._last_write_monotonic: float | None = None
        self.fallback = False

    @property
    def last_setpoint_w(self) -> float | None:
        """The last setpoint written this CHARGE window, if any."""
        return self._last_written_w

    def entry_setpoint_w(self) -> float:
        """
        Return the setpoint the executor writes on CHARGE entry.

        The loop's current computed value when both sensors read, the
        conservative fallback otherwise. The caller (executor) hands
        it to the driver inside the transition sequence; `mark_written`
        must follow so the loop's deadband baseline matches reality.
        """
        target = self._compute()
        return CHARGE_FALLBACK_W if target is None else target

    def mark_written(self, watts: float, now: float) -> None:
        """Record a setpoint someone else wrote (the CHARGE entry)."""
        self._last_written_w = watts
        self._last_write_monotonic = now

    def _compute(self) -> float | None:
        grid_import_w, battery_charge_w = self._get_inputs()
        if grid_import_w is None or battery_charge_w is None:
            self.fallback = True
            return None
        self.fallback = False
        return charge_setpoint_w(grid_import_w, battery_charge_w)

    async def on_update(self, now: float) -> None:
        """
        Recompute on a meter update; write when it matters.

        `now` is monotonic seconds (injected for testability). Writes
        are skipped outside CHARGE, inside the deadband, or before the
        minimum interval since the last write has passed.
        """
        if not self._is_charging():
            # Next CHARGE entry starts from a fresh baseline.
            self._last_written_w = None
            self._last_write_monotonic = None
            return
        target = self._compute()
        if target is None:
            target = CHARGE_FALLBACK_W
        if (
            self._last_written_w is not None
            and abs(target - self._last_written_w) < DEADBAND_W
        ):
            return
        if (
            self._last_write_monotonic is not None
            and now - self._last_write_monotonic < MIN_WRITE_INTERVAL_S
        ):
            return
        try:
            await self._driver.set_charge_power(target)
        except DriverError:
            # Real Modbus trouble; the executor's next tick owns the
            # health latch. The failed value is not recorded, so the
            # next event retries.
            return
        self._last_written_w = target
        self._last_write_monotonic = now
