"""
Shared plan types, constraint validation and single-day evaluation.

Pure module: no I/O, no clock reads, no Home Assistant imports
(ADR-0001). Units are explicit throughout: powers in W (grid side for
charge, meter side for discharge), energies in kWh, prices in EUR/kWh,
interval length in hours.

Constraint IDs (C-1..C-8) refer to docs/spec.md §6. The efficiency
model is C-6: eta_c = eta_d = sqrt(eta_roundtrip); the wear cost is
booked per kWh discharged at the meter, consistent with C-8's pairing
condition price[d] > price[c]/eta_rt + WEAR_COST.

`saving_vs_no_cycling` is the single-day evaluator the backtest
(Task 6) reuses: it counts intra-day cash flows only. Energy left in
the battery at end of day carries no value here — the multi-day
chaining in Task 6 (end SoC becoming the next day's start SoC) is
what values it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .calendar import TZ_PORTUGAL, period

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import date, tzinfo

# Numeric slack for float comparisons in the validator: refers to
# quantities in W or kWh, far below any physically meaningful amount.
_EPS = 1e-6


@dataclass(frozen=True)
class BatteryParams:
    """
    Battery and installation parameters (defaults: CONTEXT.md).

    `cap_usable_kwh` is deliberately a parameter, never a constant in
    strategy code — Checkpoint B evaluates the second unit by doubling
    it. `soc_start_kwh=None` means the day starts at the reserve floor.
    """

    cap_usable_kwh: float = 5.0
    cap_min_kwh: float = 1.35
    p_charge_max_w: float = 2000.0
    p_discharge_max_w: float = 2500.0
    p_usable_w: float = 4400.0
    eta_roundtrip: float = 0.90
    wear_cost_eur_kwh: float = 0.020
    soc_start_kwh: float | None = None
    interval_hours: float = 0.25

    @property
    def eta_one_way(self) -> float:
        """Charge/discharge one-way efficiency (C-6)."""
        return self.eta_roundtrip**0.5

    @property
    def start_soc_kwh(self) -> float:
        """Initial SoC: explicit value, or the reserve floor."""
        return self.cap_min_kwh if self.soc_start_kwh is None else self.soc_start_kwh


@dataclass(frozen=True)
class Plan:
    """Per-interval setpoints: grid-side charge W, meter-side discharge W."""

    charge_w: tuple[float, ...]
    discharge_w: tuple[float, ...]

    def __post_init__(self) -> None:
        """Reject mismatched vector lengths."""
        if len(self.charge_w) != len(self.discharge_w):
            msg = "charge_w and discharge_w must have the same length"
            raise ValueError(msg)

    def __len__(self) -> int:
        """Return the number of intervals."""
        return len(self.charge_w)


def soc_trajectory(plan: Plan, params: BatteryParams) -> list[float]:
    """
    SoC in kWh at interval boundaries; element 0 is the start SoC.

    C-6: SoC[i+1] = SoC[i] + charge_e*eta_c - discharge_e/eta_d.
    """
    eta = params.eta_one_way
    dt = params.interval_hours
    soc = [params.start_soc_kwh]
    for charge, discharge in zip(plan.charge_w, plan.discharge_w, strict=True):
        delta = charge * dt / 1000 * eta - discharge * dt / 1000 / eta
        soc.append(soc[-1] + delta)
    return soc


def validate_plan(
    plan: Plan,
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
) -> list[str]:
    """Check C-1..C-7; return violation messages (empty list = valid)."""
    violations: list[str] = []
    if len(plan) != len(load_w) or len(plan) != len(solar_w):
        return [f"length mismatch: plan {len(plan)}, load {len(load_w)}"]
    for i, (charge, discharge) in enumerate(
        zip(plan.charge_w, plan.discharge_w, strict=True)
    ):
        net_load = max(0.0, load_w[i] - solar_w[i])
        if charge < -_EPS or discharge < -_EPS:
            violations.append(f"[{i}] negative setpoint")
        if discharge > net_load + _EPS:
            violations.append(
                f"C-1[{i}] discharge {discharge:.0f} > net {net_load:.0f}"
            )
        if discharge > params.p_discharge_max_w + _EPS:
            violations.append(f"C-2[{i}] discharge above P_DIS_MAX")
        if charge > min(params.p_charge_max_w, params.p_usable_w - load_w[i]) + _EPS:
            violations.append(f"C-3[{i}] charge {charge:.0f} above limit")
        if charge > _EPS and discharge > _EPS:
            violations.append(f"C-7[{i}] simultaneous charge and discharge")
    for i, soc in enumerate(soc_trajectory(plan, params)):
        if soc < params.cap_min_kwh - _EPS:
            violations.append(f"C-4[{i}] SoC {soc:.3f} below reserve floor")
        if soc > params.cap_usable_kwh + _EPS:
            violations.append(f"C-5[{i}] SoC {soc:.3f} above usable capacity")
    return violations


def _runs[T](values: Sequence[T | None]) -> Iterator[tuple[int, int, T]]:
    """Yield (start, end, value) for maximal runs of equal non-None values."""
    current: T | None = None
    start = 0
    for i in range(len(values) + 1):
        value = values[i] if i < len(values) else None
        if value == current:
            continue
        if current is not None:
            yield (start, i, current)
        current = value
        start = i


def schedule_segments(
    day: date,
    charge_w: Sequence[float],
    discharge_w: Sequence[float],
    tz: tzinfo = TZ_PORTUGAL,
    interval: timedelta = timedelta(minutes=15),
) -> list[dict[str, str | float]]:
    """
    Merge a day's parallel power vectors into schedule segments.

    Returns `[{"start", "end", "direction", "power_w"}, ...]`:
    consecutive intervals merge while both the direction
    ("charge"/"discharge") and the rounded power hold; hold intervals
    produce no segment. Times are the vectors' own wall-clock
    convention localised to `tz`, ISO 8601 with offset — so lists
    built for consecutive days concatenate into one multi-day
    schedule.
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    actions: list[tuple[str, float] | None] = []
    for charge, discharge in zip(charge_w, discharge_w, strict=True):
        if charge > 0:
            actions.append(("charge", round(charge, 1)))
        elif discharge > 0:
            actions.append(("discharge", round(discharge, 1)))
        else:
            actions.append(None)
    return [
        {
            "start": (midnight + start * interval).isoformat(),
            "end": (midnight + end * interval).isoformat(),
            "direction": direction,
            "power_w": power,
        }
        for start, end, (direction, power) in _runs(actions)
    ]


def price_segments(
    day: date,
    prices_eur_kwh: Sequence[float],
    tz: tzinfo = TZ_PORTUGAL,
    interval: timedelta = timedelta(minutes=15),
) -> list[dict[str, str | float]]:
    """
    Merge a day's delivered-price vector into display segments.

    Returns `[{"start", "end", "price_eur_kwh", "tar_period"}, ...]`:
    consecutive intervals merge while both the rounded price and the
    TAR period hold — quarter-hourly OMIE rarely repeats, but flat or
    hourly-padded stretches collapse, and the period boundary always
    starts a fresh segment so each one is checkable against the
    tariff table. Same timestamp convention as `schedule_segments`,
    so consecutive days concatenate.
    """
    # Naive = Portugal legal time, the calendar API contract.
    naive_midnight = datetime(day.year, day.month, day.day)  # noqa: DTZ001
    values: list[tuple[float, str]] = [
        (round(p, 5), period(naive_midnight + i * interval))
        for i, p in enumerate(prices_eur_kwh)
    ]
    midnight = naive_midnight.replace(tzinfo=tz)
    return [
        {
            "start": (midnight + start * interval).isoformat(),
            "end": (midnight + end * interval).isoformat(),
            "price_eur_kwh": value,
            "tar_period": tar,
        }
        for start, end, (value, tar) in _runs(values)
    ]


def saving_vs_no_cycling(
    plan: Plan,
    prices: Sequence[float],
    params: BatteryParams,
) -> float:
    """
    EUR saved on the day versus not cycling at all.

    sum(price * discharged) - sum(price * charged) - wear * discharged,
    all energies at the meter. Fixed terms and VAT are uniform and
    excluded (spec §4); end-of-day stored energy is not valued here.
    """
    dt = params.interval_hours
    saving = 0.0
    for i, (charge, discharge) in enumerate(
        zip(plan.charge_w, plan.discharge_w, strict=True)
    ):
        discharge_kwh = discharge * dt / 1000
        charge_kwh = charge * dt / 1000
        saving += prices[i] * (discharge_kwh - charge_kwh)
        saving -= params.wear_cost_eur_kwh * discharge_kwh
    return saving
