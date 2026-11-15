"""
Static seasonal schedule — the reference baseline and production fallback.

Consults no prices. The rules, from docs/plan.md Task 5:

- charge in vazio (the stretch before the day's first discharge:
  00:00-07:00 weekly) November-April, and at midday (13:00-17:00)
  May-October — the seasonal inversion follows the month, matching
  the docs' wording "vazio Nov-Apr, midday May-Oct";
- discharge during ponta at the net load (zero-export), while energy
  above the reserve floor remains;
- days without ponta under the weekly cycle (Saturday, Sunday) do
  nothing: charging into a day with no discharge window would strand
  the energy.

Generalised by the tariff (ADR-0012): the discharge periods are the
tariff's (`ponta` for tri, all of fora de vazio for bi, none for
simples), a day with no discharge interval is a no-op day, and
bi-horária always charges overnight. Without a tariff — or with the
default tri-horária weekly one — the schedule is exactly the above.

Single-day semantics: the plan starts at params.start_soc_kwh. The
summer schedule charges at midday AFTER the morning ponta, so its
steady state relies on multi-day chaining (end SoC feeding the next
day's start): the backtest chains in Task 6's harness, and production
chains through `chained_start_soc` below — a summer day seeded at the
floor simply cannot serve its morning ponta. Intervals are indexed
from local midnight at params.interval_hours, ELAPSED time
(`calendar.interval_starts`): under the weekly cycle the DST switch
days are Sundays (no-op days), and under the daily cycle the intervals
after the switch keep their true wall-clock period.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import TYPE_CHECKING

from .calendar import CALENDARS, Calendars, Period, interval_starts, period
from .plan import ARMED_CHARGE_KWH, BatteryParams, Plan, soc_trajectory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from .tariff import Tariff

# Months whose charging window is overnight vazio; the rest use midday.
VAZIO_CHARGE_MONTHS = frozenset({11, 12, 1, 2, 3, 4})
MIDDAY_CHARGE_START_H = 13.0
MIDDAY_CHARGE_END_H = 17.0
_TRI_DISCHARGE: frozenset[Period] = frozenset({"ponta"})


def static_plan(  # noqa: PLR0913 - `tariff` is keyword-only
    day: date,
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
    calendars: Calendars = CALENDARS,
    *,
    tariff: Tariff | None = None,
) -> Plan:
    """
    Build the fixed seasonal plan for one day. No prices consulted.

    `tariff` (ADR-0012) supplies the calendar and the discharge
    periods; without it the plan is the tri-horária one on `calendars`
    (the pre-ADR-0012 schedule, unchanged).
    """
    n = len(load_w)
    dt = params.interval_hours
    eta = params.eta_one_way
    charge = [0.0] * n
    discharge = [0.0] * n

    if tariff is not None:
        calendars = tariff.calendars
    discharge_periods = _discharge_periods(tariff)
    starts = interval_starts(day, n, dt)
    periods = [period(when, calendars) for when in starts]
    discharging = [name in discharge_periods for name in periods]
    if not any(discharging):  # e.g. weekly Sat/Sun: nothing to serve
        return Plan(charge_w=tuple(charge), discharge_w=tuple(discharge))

    first_discharge = discharging.index(True)
    charge_overnight = _charges_overnight(day, tariff)
    soc = params.start_soc_kwh
    window: list[int] = []
    for i in range(n):
        hour = starts[i].hour + starts[i].minute / 60

        if discharging[i]:
            net_load = max(0.0, load_w[i] - solar_w[i])
            available_kwh = max(0.0, soc - params.cap_min_kwh) * eta
            discharge[i] = min(
                params.p_discharge_max_w, net_load, available_kwh / dt * 1000
            )
            soc -= discharge[i] * dt / 1000 / eta
            continue

        # Overnight: the vazio stretch before the day's first discharge
        # (00:00-07:00 weekly, 00:00-08:00 daily).
        in_window = (
            periods[i] == "vazio" and i < first_discharge
            if charge_overnight
            else MIDDAY_CHARGE_START_H <= hour < MIDDAY_CHARGE_END_H
        )
        if in_window:
            window.append(i)
            headroom_kwh = max(0.0, params.cap_usable_kwh - soc) / eta
            charge[i] = min(
                params.p_charge_max_w,
                max(0.0, params.p_usable_w - load_w[i]),
                headroom_kwh / dt * 1000,
            )
            soc += charge[i] * dt / 1000 * eta

    _arm_window_tail(charge, window, load_w, params)
    return Plan(charge_w=tuple(charge), discharge_w=tuple(discharge))


def _discharge_periods(tariff: Tariff | None) -> frozenset[Period]:
    """Return the periods the schedule discharges into: ponta unless the tariff says."""
    return _TRI_DISCHARGE if tariff is None else tariff.discharge_periods


def _charges_overnight(day: date, tariff: Tariff | None) -> bool:
    """
    Return True when the day's charge window is overnight vazio.

    Tri-horária follows the month (vazio Nov-Apr, midday cheias May-Oct:
    the summer solar trough). Bi-horária always charges in vazio — its
    midday is fora de vazio, priced like the discharge it would serve.
    """
    if tariff is not None and tariff.option == "bi":
        return True
    return day.month in VAZIO_CHARGE_MONTHS


def is_active_day(
    day: date,
    params: BatteryParams,
    calendars: Calendars = CALENDARS,
    *,
    tariff: Tariff | None = None,
) -> bool:
    """Return True when `day` has at least one interval to discharge into."""
    if tariff is not None:
        calendars = tariff.calendars
    discharge_periods = _discharge_periods(tariff)
    if not discharge_periods:
        return False
    n = round(24 / params.interval_hours)
    return any(
        period(when, calendars) in discharge_periods
        for when in interval_starts(day, n, params.interval_hours)
    )


def _arm_window_tail(
    charge: list[float],
    window: list[int],
    load_w: Sequence[float],
    params: BatteryParams,
) -> None:
    """
    Hold the CHARGE state through the whole seasonal window.

    Owner decision 2026-08-13 (spec §8): quarters the model leaves
    empty once it reaches capacity are armed at a marginal power — a
    state selector (ADR-0007), never an energy claim — shaved off the
    filled quarters so totals, the end SoC and the day-chaining seed
    are unchanged. At run time the charge loop drives full power and
    the firmware percent-target stops at ACTUAL full, so real-world
    shortfalls (anti-feed discharged a deeper-than-forecast load; the
    loop was throttled under house load) recover in the remaining
    window time instead of persisting into the next ponta.
    """
    dt = params.interval_hours
    armed_w = ARMED_CHARGE_KWH / dt * 1000
    empty = [
        i
        for i in window
        if charge[i] <= 0.0
        and min(params.p_charge_max_w, params.p_usable_w - load_w[i]) >= 1.0
    ]
    donors = [i for i in window if charge[i] > 0.0]
    if not empty or not donors:
        return
    # Donors always precede the empties (the sequential fill above is
    # front-loaded), so the shave only moves energy LATER — the
    # modelled trajectory can only drop toward the ceiling, never rise.
    available = sum(max(0.0, charge[d] - armed_w) for d in donors)
    armed = empty[: int(available / armed_w)]
    need = armed_w * len(armed)
    for i in armed:
        charge[i] = armed_w
    for d in reversed(donors):
        take = min(charge[d] - armed_w, need)
        if take <= 0.0:
            continue
        charge[d] -= take
        need -= take
        if need <= 0.0:
            break


def chained_start_soc(  # noqa: PLR0913 - `tariff` is keyword-only
    day: date,
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
    calendars: Calendars = CALENDARS,
    *,
    tariff: Tariff | None = None,
) -> float:
    """
    Start SoC (kWh) for `day` under virtual day-chaining.

    The single-day summer plan cannot serve its morning ponta (the
    charge window is midday, after it), so production seeds each day
    from the PREVIOUS ACTIVE DAY'S planned end SoC — the plan's own model
    rolled forward, never a SoC readback (ADR-0008). Days with nothing
    to discharge into (weekly Sat/Sun) are no-op days whose FLOWS pass
    SoC through — but the standby drain keeps running (owner
    2026-08-17), so each intervening no-op day subtracts 24 h of
    `self_discharge_w`, clamped at the floor like everywhere else. A
    Monday genuinely starts ~2 x 0.46 kWh below Friday's end.

    One floor-seeded simulation of that day is exact, not an
    approximation: an active day's end SoC is start-independent, because
    its charge window fills to usable capacity from any start (winter
    charges first, making the rest of the day deterministic; summer
    charges last, ending full either way — the drain does not break
    this: it is deterministic given the plan). Uses `day`'s own load
    vector for the simulation — with the flat production load this is
    exact; with a per-day forecast it is the model's best stand-in
    for a day already past.

    A tariff with no discharge period at all (simples) has no active
    day to chain from: the seed is the floor.
    """
    if tariff is not None and not tariff.discharge_periods:
        return params.cap_min_kwh
    previous = day - timedelta(days=1)
    for _ in range(7):
        if is_active_day(previous, params, calendars, tariff=tariff):
            break
        previous -= timedelta(days=1)
    floor_seeded = dataclasses.replace(params, soc_start_kwh=None)
    plan = static_plan(
        previous, load_w, solar_w, floor_seeded, calendars, tariff=tariff
    )
    end = soc_trajectory(plan, floor_seeded, include_self_discharge=True)[-1]
    gap_days = (day - previous).days - 1
    gap_drain_kwh = params.self_discharge_w * 24.0 / 1000.0 * gap_days
    return max(params.cap_min_kwh, end - gap_drain_kwh)
