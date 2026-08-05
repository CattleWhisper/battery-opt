"""
Backtest harness (Task 6): replay strategies over historical OMIE data.

Chains days: each day's ending SoC becomes the next day's start — this
is what values energy carried across midnight (the core evaluator is
deliberately single-day; the summer static schedule only works in this
steady state). Cost accounting is the Task 5 evaluator, never
reimplemented here.

Price models:
- horaria  (default): quarter-hourly OMIE through the EDP formula,
  K1 = 1.08. Nothing assumes prices are positive.
- media    (--monthly): the billing-window [day 2, day 2) per-period
  average OMIE through the same formula, K1 = 1.10 — Indexada Média
  bills the monthly average, so intra-period selection is worthless.
- simples-15 (--tariff simples-15): the current fixed tariff at
  EUR 0.1420/kWh flat (CONTEXT.md §Current tariff state). Flat prices
  give the optimiser nothing to pair — the battery idles, as it does
  in reality on this tariff. Fixed daily terms are assumed equal to
  the indexed tariff's (K3 + TAR potencia): the comparison is
  dominated by the energy component; noted in docs/findings.md.

Usage examples (Checkpoint B, docs/plan.md):
    python backtest/run.py --strategy greedy
    python backtest/run.py --strategy static
    python backtest/run.py --strategy greedy --cap 10
    python backtest/run.py --strategy greedy --k1 1.10 --monthly
    python backtest/run.py --strategy greedy --tariff simples-15
    python backtest/run.py --strategy greedy --resolution hourly
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from backtest.load_omie import (
    DATA_DIR,
    PriceRecord,
    load_series,
    window_period_averages,
)
from backtest.report import DayResult, annualize, write_csv
from custom_components.battery_opt.core.calendar import period, season
from custom_components.battery_opt.core.optimiser import solve
from custom_components.battery_opt.core.plan import (
    BatteryParams,
    Plan,
    saving_vs_no_cycling,
    soc_trajectory,
    validate_plan,
)
from custom_components.battery_opt.core.prices import (
    K1_HORARIA,
    price,
)
from custom_components.battery_opt.core.static_schedule import static_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

BASE_LOAD_W = 1040.0  # CONTEXT.md: flat 24/7 load
SIMPLES_15_EUR_KWH = 0.1420  # CONTEXT.md: Simples at the 15% discount

# Default Checkpoint B window: the 11 quarter-hourly months. Sep 2025
# is hourly-only and is reported separately (docs/plan.md).
DEFAULT_FIRST_DAY = date(2025, 10, 1)
DEFAULT_LAST_DAY = date(2026, 8, 6)


class PriceModel(Protocol):
    """Maps one market record to a delivered EUR/kWh price."""

    def __call__(self, record: PriceRecord) -> float:
        """Return the delivered price for the record's interval."""
        ...


def horaria_price_model(k1: float = K1_HORARIA) -> PriceModel:
    """Indexada Horária: each interval at its own OMIE price."""

    def model(record: PriceRecord) -> float:
        return price(record.price_eur_mwh, record.start, k1=k1)

    return model


def media_price_model(records: list[PriceRecord], k1: float) -> PriceModel:
    """
    Indexada Média: billing-window per-period average OMIE.

    Windows follow the EDP convention [day 2 of M, day 2 of M+1) —
    see docs/tariff-reference.md §5.
    """
    averages: dict[tuple[date, date], dict[str, float]] = {}

    def window_of(day: date) -> tuple[date, date]:
        anchor = day if day.day >= 2 else _previous_month_day2(day)
        start = date(anchor.year, anchor.month, 2)
        nxt = date(start.year + 1, 1, 2) if start.month == 12 else None
        end = nxt or date(start.year, start.month + 1, 2)
        return (start, end)

    def model(record: PriceRecord) -> float:
        window = window_of(record.start.date())
        if window not in averages:
            averages[window] = window_period_averages(records, *window)
        omie_avg = averages[window][period(record.start)]
        return price(omie_avg, record.start, k1=k1)

    return model


def _previous_month_day2(day: date) -> date:
    if day.month == 1:
        return date(day.year - 1, 12, 2)
    return date(day.year, day.month - 1, 2)


def simples_15_price_model() -> PriceModel:
    """Price every interval at the flat Simples retail rate."""

    def model(record: PriceRecord) -> float:  # noqa: ARG001
        return SIMPLES_15_EUR_KWH

    return model


def group_by_local_day(
    records: list[PriceRecord],
    boundary_hour: int = 0,
) -> dict[date, list[PriceRecord]]:
    """
    Bucket records by Lisbon-local planning day, sorted within each.

    `boundary_hour` shifts where the planning day starts (13 = plan
    13:00-to-13:00): used to quantify open question #4, since the
    per-day greedy cannot pair yesterday's midday trough with this
    morning's ponta across a midnight boundary. The bucket key is the
    date on which the planning day STARTS.
    """
    groups: dict[date, list[PriceRecord]] = defaultdict(list)
    for record in records:
        groups[(record.start - timedelta(hours=boundary_hour)).date()].append(record)
    return {
        day: sorted(day_records, key=lambda r: r.start)
        for day, day_records in groups.items()
    }


def to_hourly(day_records: list[PriceRecord]) -> list[PriceRecord]:
    """
    Aggregate quarter-hourly records to hourly means (--resolution).

    Quantifies what quarter-hourly resolution is worth: the same day
    replayed as if only hourly prices existed.
    """
    by_hour: dict[tuple[date, int], list[PriceRecord]] = defaultdict(list)
    for record in day_records:
        by_hour[(record.start.date(), record.start.hour)].append(record)
    hourly = []
    for chunk in by_hour.values():
        total_hours = sum(r.duration_hours for r in chunk)
        mean_price = (
            sum(r.price_eur_mwh * r.duration_hours for r in chunk) / total_hours
        )
        hourly.append(
            PriceRecord(
                start=min(r.start for r in chunk),
                duration_hours=total_hours,
                price_eur_mwh=mean_price,
            )
        )
    return sorted(hourly, key=lambda r: r.start)


# Strategy dispatch mirrors the shared (prices, load, solar, params)
# vector contract; collapsing them into a bundle would obscure it.
def _plan_for(  # noqa: PLR0913
    strategy: str,
    day: date,
    prices_kwh: Sequence[float],
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
) -> Plan:
    if strategy == "greedy":
        return solve(prices_kwh, load_w, solar_w, params).plan
    if strategy == "static":
        return static_plan(day, load_w, solar_w, params)
    if strategy == "none":
        return Plan(charge_w=(0.0,) * len(load_w), discharge_w=(0.0,) * len(load_w))
    msg = f"Unknown strategy: {strategy!r}"
    raise ValueError(msg)


def simulate(
    days: list[tuple[date, list[PriceRecord]]],
    strategy: str,
    params: BatteryParams,
    price_model: PriceModel | None = None,
    plan_wear: float | None = None,
) -> list[DayResult]:
    """
    Replay a strategy over consecutive days, chaining the SoC.

    `plan_wear` (the cheias-cycling cap from the Checkpoint B
    decisions) makes the optimiser PLAN with an inflated wear cost —
    pruning the least profitable cycles first — while savings are
    always evaluated at the true `params.wear_cost_eur_kwh`.
    """
    model = price_model or horaria_price_model()
    soc = params.start_soc_kwh
    results: list[DayResult] = []
    for day, day_records in days:
        n = len(day_records)
        dt = day_records[0].duration_hours
        prices_kwh = [model(record) for record in day_records]
        load_w = [BASE_LOAD_W] * n
        solar_w = [0.0] * n
        day_params = dataclasses.replace(params, soc_start_kwh=soc, interval_hours=dt)
        solve_params = (
            dataclasses.replace(day_params, wear_cost_eur_kwh=plan_wear)
            if plan_wear is not None
            else day_params
        )
        plan = _plan_for(strategy, day, prices_kwh, load_w, solar_w, solve_params)
        violations = validate_plan(plan, load_w, solar_w, day_params)
        if violations:
            msg = f"{strategy} plan invalid on {day}: {violations[:3]}"
            raise RuntimeError(msg)
        saving = saving_vs_no_cycling(plan, prices_kwh, day_params)
        eta = day_params.eta_one_way
        consumption = sum(load_w) * dt / 1000
        base_cost = sum(
            p * w * dt / 1000 for p, w in zip(prices_kwh, load_w, strict=True)
        )
        charge_kwh = sum(plan.charge_w) * dt / 1000
        discharge_kwh = sum(plan.discharge_w) * dt / 1000
        billed_saving = saving + day_params.wear_cost_eur_kwh * discharge_kwh
        soc = soc_trajectory(plan, day_params)[-1]
        results.append(
            DayResult(
                day=day,
                season=season(day),
                intervals=n,
                interval_hours=dt,
                consumption_kwh=consumption,
                base_cost_eur=base_cost,
                cost_eur=base_cost - billed_saving,
                saving_eur=saving,
                charge_kwh=charge_kwh,
                discharge_kwh=discharge_kwh,
                discharge_battery_kwh=discharge_kwh / eta,
                end_soc_kwh=soc,
            )
        )
    return results


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy", choices=["greedy", "static", "none"], default="greedy"
    )
    parser.add_argument(
        "--cap", type=float, default=5.0, help="usable capacity kWh (10 = second unit)"
    )
    parser.add_argument(
        "--k1",
        type=float,
        default=None,
        help="override K1 (default: 1.08 hourly, 1.10 monthly)",
    )
    parser.add_argument(
        "--hourly", action="store_true", help="Indexada Horária billing (default)"
    )
    parser.add_argument(
        "--monthly",
        action="store_true",
        help="Indexada Média billing (per-period window average)",
    )
    parser.add_argument(
        "--tariff", choices=["horaria-tri", "simples-15"], default="horaria-tri"
    )
    parser.add_argument("--wear", type=float, default=0.020)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_FIRST_DAY)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_LAST_DAY)
    parser.add_argument(
        "--resolution",
        choices=["native", "hourly"],
        default="native",
        help="'hourly' degrades quarter-hours to hour means",
    )
    parser.add_argument(
        "--csv", type=Path, default=None, help="write per-day rows to this path"
    )
    parser.add_argument(
        "--day-boundary-hour",
        type=int,
        default=0,
        help="planning-day start hour (open question #4); greedy only",
    )
    parser.add_argument(
        "--plan-wear",
        type=float,
        default=None,
        help="optimiser plans with this wear cost (cycle-cap lever); "
        "savings always evaluated at --wear",
    )
    return parser


def _price_model_from_args(
    args: argparse.Namespace, records: list[PriceRecord]
) -> PriceModel:
    if args.tariff == "simples-15":
        return simples_15_price_model()
    if args.monthly:
        return media_price_model(records, k1=args.k1 or 1.10)
    return horaria_price_model(k1=args.k1 or K1_HORARIA)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one backtest configuration and print the annual summary."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.day_boundary_hour and args.strategy == "static":
        parser.error(
            "--day-boundary-hour applies to the greedy only; "
            "the static schedule is calendar-day-indexed"
        )
    started = time.monotonic()
    records = load_series(DATA_DIR, args.start, args.end)
    groups = group_by_local_day(records, args.day_boundary_hour)
    days = [
        (day, groups[day]) for day in sorted(groups) if args.start <= day <= args.end
    ]
    if args.resolution == "hourly":
        days = [(day, to_hourly(day_records)) for day, day_records in days]
    params = BatteryParams(cap_usable_kwh=args.cap, wear_cost_eur_kwh=args.wear)
    model = _price_model_from_args(args, records)
    results = simulate(days, args.strategy, params, model, plan_wear=args.plan_wear)
    # The static comparison only makes sense on calendar days: the
    # static schedule is day-indexed, so shifted planning windows
    # would feed it garbage.
    static_results = (
        simulate(days, "static", params, model)
        if args.strategy == "greedy" and not args.day_boundary_hour
        else None
    )
    annual = annualize(results, params)
    print(
        f"strategy={args.strategy} tariff={args.tariff} cap={args.cap} "
        f"billing={'media' if args.monthly else 'horaria'} "
        f"resolution={args.resolution} "
        f"window={days[0][0]}..{days[-1][0]} ({len(days)} days)"
    )
    for key in sorted(annual):
        print(f"  {key:34} {annual[key]:12.2f}")
    if static_results is not None:
        static_annual = annualize(static_results, params)
        delta = (
            annual["annual_saving_eur_incl_vat"]
            - static_annual["annual_saving_eur_incl_vat"]
        )
        print(
            f"  {'static_saving_eur_incl_vat':34} "
            f"{static_annual['annual_saving_eur_incl_vat']:12.2f}"
        )
        print(f"  {'dynamic_gain_vs_static_incl_vat':34} {delta:12.2f}")
    if args.csv:
        write_csv(results, args.csv)
        print(f"per-day rows written to {args.csv}")
    print(f"elapsed: {time.monotonic() - started:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
