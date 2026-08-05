"""
Aggregation and reporting for the backtest (Task 6).

Annualization is season-weighted: the simulated window (Oct 1 2025 -
Aug 6 2026) over-represents winter relative to a real year, so naive
x365/days scaling would inflate the (ponta-heavy) winter contribution.
Per-season daily means are scaled by the season's share of the
reference year Sep 2025 - Aug 2026 (154 winter / 211 summer days,
computed from the calendar, not hardcoded).

Cost figures follow spec §4: per-day figures exclude fixed terms and
VAT (they cannot change the optimum); the annual billed cost adds
(K3 + TAR potencia) x 365 and VAT via `total_daily_cost` for
reporting. `saving_eur` is net of wear (the evaluator's number);
`cost_eur` is the billed energy cost, which does not include wear.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from typing import TYPE_CHECKING

from custom_components.battery_opt.core.calendar import season
from custom_components.battery_opt.core.prices import total_daily_cost

if TYPE_CHECKING:
    from pathlib import Path

    from custom_components.battery_opt.core.plan import BatteryParams

_REFERENCE_YEAR_START = date(2025, 9, 1)


@dataclass(frozen=True)
class DayResult:
    """One simulated day of one strategy."""

    day: date
    season: str
    intervals: int
    interval_hours: float
    consumption_kwh: float
    base_cost_eur: float  # energy cost with no battery, excl fixed/VAT
    cost_eur: float  # billed energy cost with battery, excl fixed/VAT
    saving_eur: float  # net of wear (core evaluator), excl fixed/VAT
    charge_kwh: float  # grid side
    discharge_kwh: float  # meter side
    discharge_battery_kwh: float  # SoC side (meter / eta_d) - cycle counting
    end_soc_kwh: float


def season_weights() -> dict[str, int]:
    """Days per season in the reference year Sep 2025 - Aug 2026."""
    weights = {"winter": 0, "summer": 0}
    for offset in range(365):
        weights[season(_REFERENCE_YEAR_START + timedelta(days=offset))] += 1
    return weights


def annualize(results: list[DayResult], params: BatteryParams) -> dict[str, float]:
    """Season-weighted annual metrics from per-day results."""
    weights = season_weights()
    annual: dict[str, float] = {
        "days_simulated": len(results),
        "annual_consumption_kwh": 0.0,
        "annual_base_cost_eur": 0.0,
        "annual_cost_eur": 0.0,
        "annual_saving_eur": 0.0,
        "annual_charge_kwh": 0.0,
        "annual_discharge_kwh": 0.0,
        "annual_discharge_battery_kwh": 0.0,
    }
    per_field = {
        "annual_consumption_kwh": "consumption_kwh",
        "annual_base_cost_eur": "base_cost_eur",
        "annual_cost_eur": "cost_eur",
        "annual_saving_eur": "saving_eur",
        "annual_charge_kwh": "charge_kwh",
        "annual_discharge_kwh": "discharge_kwh",
        "annual_discharge_battery_kwh": "discharge_battery_kwh",
    }
    for season_name, days_per_year in weights.items():
        in_season = [r for r in results if r.season == season_name]
        if not in_season:
            continue
        for metric, field_name in per_field.items():
            mean_daily = sum(getattr(r, field_name) for r in in_season) / len(in_season)
            annual[metric] += mean_daily * days_per_year
    annual["annual_saving_eur_incl_vat"] = annual["annual_saving_eur"] * 1.23
    annual["annual_billed_cost_eur_incl_vat"] = total_daily_cost(
        annual["annual_cost_eur"], days=365
    )
    annual["cycles_per_year"] = (
        annual["annual_discharge_battery_kwh"] / params.cap_usable_kwh
    )
    return annual


def write_csv(results: list[DayResult], path: Path) -> None:
    """Write per-day rows for inspection."""
    columns = [f.name for f in fields(DayResult)]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))
