r"""
What-if: a large battery charged only in each day's cheapest quarters.

A sizing/strategy sketch, deliberately simpler than `core/optimiser.py`:
for each day, buy the whole day's usable energy in that day's CHEAPEST
quarter-hours, serve the house from the battery for the rest of the
day, and price whatever the battery cannot cover at a flat
`--excess-price`. No day-chaining, no wear, no ponta targeting — the
question here is "how big a bill would a big battery leave", not "what
would the planner do".

Prices come from OMIE through the project's own delivered-price
formula (`core.prices.price`), so they are the same EUR/kWh the
integration would compute. Days missing from the local OMIE cache are
downloaded on demand (`backtest/download_omie.py`).

Two constraints keep the answer honest rather than a pure price sort:

- **Round-trip efficiency.** Delivering X kWh from the battery costs
  X / eta at the meter, so the cheap-window purchase is always larger
  than the energy it serves.
- **Contracted power.** Charging cannot exceed `--p-usable` minus the
  house load already drawn in that quarter, so the cheapest quarters
  have finite room and a big battery may simply not fit its charge
  into one night. Shortfall spills to `--excess-price`, which is what
  makes the result sensitive to capacity rather than linear in it.

Usage:
    python backtest/simulate_big_battery.py --meter ~/Downloads/history.csv \
        --capacity 20 --from 2026-08-31 --to 2026-09-08
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.download_omie import DATA_DIR, download
from backtest.load_omie import load_series
from backtest.simulate_bill import load_meter, quarter_index
from custom_components.battery_opt.core.prices import price

QUARTERS_PER_DAY = 96
QUARTER_HOURS = 0.25

# CONTEXT.md: 4600 VA contracted, 200 W margin -> 4400 W usable for
# house load plus charging together.
DEFAULT_P_USABLE_W = 4400.0
DEFAULT_ETA_ROUNDTRIP = 0.90


@dataclass(frozen=True)
class BatterySpec:
    """The what-if being simulated: pack, losses, and the power ceiling."""

    capacity_kwh: float
    excess_price: float
    eta_roundtrip: float = DEFAULT_ETA_ROUNDTRIP
    p_usable_w: float = DEFAULT_P_USABLE_W


@dataclass
class DayResult:
    """One simulated day."""

    day: date
    consumption_kwh: float = 0.0
    charged_kwh: float = 0.0  # bought at the meter to fill the battery
    served_kwh: float = 0.0  # delivered out of the battery to the house
    excess_kwh: float = 0.0  # bought outside the cheap window
    charge_cost_eur: float = 0.0
    excess_cost_eur: float = 0.0
    baseline_eur: float = 0.0  # same consumption, no battery, spot prices
    quarters_used: int = 0

    @property
    def total_eur(self) -> float:
        """Simulated cost for the day."""
        return self.charge_cost_eur + self.excess_cost_eur

    @property
    def avg_charge_price(self) -> float:
        """Mean EUR/kWh actually paid to fill the battery."""
        return self.charge_cost_eur / self.charged_kwh if self.charged_kwh else 0.0


@dataclass
class Simulation:
    """Whole-window results."""

    days: list[DayResult] = field(default_factory=list)
    missing_prices: set[date] = field(default_factory=set)

    def total(self, attribute: str) -> float:
        """Sum one DayResult field across the window."""
        return sum(getattr(day, attribute) for day in self.days)


def consumption_by_quarter(
    readings: list[tuple],
) -> dict[date, list[float]]:
    """
    Bucket meter deltas into per-day, per-quarter kWh.

    Same rule as the integration's cost tracker (cost.py, decision 8):
    a negative delta is a meter reset and contributes nothing.
    """
    grid: dict[date, list[float]] = defaultdict(lambda: [0.0] * QUARTERS_PER_DAY)
    for (_, previous), (moment, current) in pairwise(readings):
        delta = current - previous
        if delta <= 0:
            continue
        grid[moment.date()][quarter_index(moment)] += delta
    return dict(grid)


def delivered_prices(
    days: list[date], omie_dir: Path, *, allow_download: bool
) -> dict[date, list[float | None]]:
    """
    Build 96 delivered prices per Lisbon day from the OMIE cache.

    A Lisbon day draws on OMIE delivery days D and D+1 (the market day
    is Madrid-local), so the fetch window is padded on both sides.
    Missing days are downloaded unless `allow_download` is False.
    """
    if not days:
        return {}
    first, last = min(days), max(days)
    if allow_download:
        download(first - timedelta(days=1), last + timedelta(days=1))
    # Day by day, not one load_series call: a single missing delivery
    # day (OMIE has not published the boundary yet, or the cache is
    # partial) must cost that day only, never the whole run.
    records = []
    day = first - timedelta(days=1)
    while day <= last + timedelta(days=1):
        try:
            records.extend(load_series(omie_dir, day, day))
        except (FileNotFoundError, ValueError) as err:
            print(f"  skipping {day}: {err}", file=sys.stderr)
        day += timedelta(days=1)
    if not records:
        return {}

    prices: dict[date, list[float | None]] = {}
    for record in records:
        local = record.start
        day = local.date()
        if day not in prices:
            prices[day] = [None] * QUARTERS_PER_DAY
        index = quarter_index(local)
        # marginalpdbc is hourly before Oct 2025 and quarter-hourly
        # after; an hourly record fills its four quarters.
        span = max(1, round(record.duration_hours / QUARTER_HOURS))
        for offset in range(span):
            if index + offset < QUARTERS_PER_DAY:
                prices[day][index + offset] = price(
                    record.price_eur_mwh,
                    local.replace(tzinfo=None) + timedelta(minutes=15 * offset),
                )
    return prices


def simulate_day(
    day: date,
    load_kwh: list[float],
    day_prices: list[float | None],
    spec: BatterySpec,
) -> DayResult:
    """
    Charge in the cheapest quarters, serve the rest of the day from it.

    The battery is sized to the day: it never buys more than the house
    will actually use. Charging is filled cheapest-quarter-first, each
    quarter limited by the contracted-power headroom left after that
    quarter's own measured load.
    """
    result = DayResult(day=day)
    result.consumption_kwh = sum(load_kwh)
    result.baseline_eur = sum(
        kwh * day_prices[q]
        for q, kwh in enumerate(load_kwh)
        if day_prices[q] is not None
    )
    if result.consumption_kwh <= 0:
        return result

    # Never store more than the day will consume, nor more than the pack holds.
    target_served = min(spec.capacity_kwh, result.consumption_kwh)
    target_charge = target_served / spec.eta_roundtrip

    order = sorted(
        (q for q in range(QUARTERS_PER_DAY) if day_prices[q] is not None),
        key=lambda q: day_prices[q],
    )
    remaining = target_charge
    for quarter in order:
        if remaining <= 1e-9:
            break
        load_w = load_kwh[quarter] / QUARTER_HOURS * 1000.0
        headroom_kwh = max(0.0, spec.p_usable_w - load_w) * QUARTER_HOURS / 1000.0
        take = min(remaining, headroom_kwh)
        if take <= 0:
            continue
        result.charged_kwh += take
        result.charge_cost_eur += take * day_prices[quarter]
        result.quarters_used += 1
        remaining -= take

    # Whatever actually fit determines what the battery can serve.
    result.served_kwh = min(
        result.charged_kwh * spec.eta_roundtrip, result.consumption_kwh
    )
    result.excess_kwh = max(0.0, result.consumption_kwh - result.served_kwh)
    result.excess_cost_eur = result.excess_kwh * spec.excess_price
    return result


def run(
    grid: dict[date, list[float]],
    prices: dict[date, list[float | None]],
    spec: BatterySpec,
) -> Simulation:
    """Simulate every day that has both meter data and prices."""
    simulation = Simulation()
    for day in sorted(grid):
        day_prices = prices.get(day)
        if day_prices is None or all(value is None for value in day_prices):
            simulation.missing_prices.add(day)
            continue
        simulation.days.append(simulate_day(day, grid[day], day_prices, spec))
    return simulation


def report(simulation: Simulation, spec: BatterySpec, simples_rate: float) -> str:
    """Render the per-day table and the window summary."""
    lines: list[str] = []
    add = lines.append
    if simulation.missing_prices:
        add(
            f"WARNING: no prices for {len(simulation.missing_prices)} day(s): "
            + ", ".join(str(d) for d in sorted(simulation.missing_prices))
        )
        add("")

    add(
        f"Per-day simulation ({spec.capacity_kwh:g} kWh battery, "
        f"excess @ {spec.excess_price:.3f} EUR/kWh)"
    )
    add(
        f"  {'day':<12}{'kWh':>8}{'from batt':>11}{'excess':>9}"
        f"{'chg EUR/kWh':>13}{'sim EUR':>10}{'spot EUR':>10}"
    )
    for day in simulation.days:
        add(
            f"  {day.day!s:<12}{day.consumption_kwh:>8.2f}{day.served_kwh:>11.2f}"
            f"{day.excess_kwh:>9.2f}{day.avg_charge_price:>13.4f}"
            f"{day.total_eur:>10.2f}{day.baseline_eur:>10.2f}"
        )
    add("")

    n = len(simulation.days)
    if not n:
        return "\n".join(lines)
    kwh = simulation.total("consumption_kwh")
    sim = simulation.total("charge_cost_eur") + simulation.total("excess_cost_eur")
    spot = simulation.total("baseline_eur")
    simples = kwh * simples_rate

    add(f"Window totals ({n} day(s), {kwh:,.1f} kWh)")
    add(
        f"  {'Simulated (big battery)':<32}{sim:>10,.2f} EUR   "
        f"{sim / kwh:>7.4f} EUR/kWh"
    )
    add(
        f"  {'Indexada, no battery (spot)':<32}{spot:>10,.2f} EUR   "
        f"{spot / kwh:>7.4f} EUR/kWh"
    )
    add(
        f"  {'Simples @ ' + format(simples_rate, '.4f'):<32}{simples:>10,.2f} EUR   "
        f"{simples_rate:>7.4f} EUR/kWh"
    )
    add("")
    add(f"  vs no battery : {sim - spot:>+10,.2f} EUR over the window")
    add(f"  vs Simples    : {sim - simples:>+10,.2f} EUR over the window")
    if n:
        add(
            f"  annualised difference vs Simples: "
            f"{(sim - simples) / n * 365:>+,.0f} EUR/yr"
        )
    add("")
    add(
        f"  battery served {simulation.total('served_kwh') / kwh * 100:.1f}% "
        f"of consumption; {simulation.total('excess_kwh'):,.1f} kWh "
        f"fell to the flat excess price"
    )
    return "\n".join(lines)


def main() -> int:
    """Parse arguments, simulate, print."""
    parser = argparse.ArgumentParser(
        description="Simulate a large battery charged in each day's cheapest hours.",
    )
    parser.add_argument("--meter", required=True, type=Path)
    parser.add_argument("--capacity", type=float, default=20.0, help="kWh usable")
    parser.add_argument(
        "--excess-price",
        type=float,
        default=0.25,
        help="EUR/kWh for energy the battery cannot cover (default 0.25)",
    )
    parser.add_argument("--eta", type=float, default=DEFAULT_ETA_ROUNDTRIP)
    parser.add_argument("--p-usable", type=float, default=DEFAULT_P_USABLE_W)
    parser.add_argument("--simples-rate", type=float, default=0.1420)
    parser.add_argument("--omie-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--no-download", action="store_true", help="never fetch missing OMIE days"
    )
    args = parser.parse_args()

    readings = load_meter(args.meter)
    if len(readings) < 2:
        print(f"Not enough usable rows in {args.meter}", file=sys.stderr)
        return 1
    grid = consumption_by_quarter(readings)
    if args.start:
        grid = {d: v for d, v in grid.items() if d >= args.start}
    if args.end:
        grid = {d: v for d, v in grid.items() if d <= args.end}
    if not grid:
        print("No consumption in the selected window.", file=sys.stderr)
        return 1

    print(
        f"Meter: {len(readings):,} rows, {len(grid)} day(s) "
        f"({min(grid)} .. {max(grid)})"
    )
    prices = delivered_prices(
        sorted(grid), args.omie_dir, allow_download=not args.no_download
    )
    if not prices:
        print("No prices available; cannot simulate.", file=sys.stderr)
        return 1

    spec = BatterySpec(
        capacity_kwh=args.capacity,
        excess_price=args.excess_price,
        eta_roundtrip=args.eta,
        p_usable_w=args.p_usable,
    )
    simulation = run(grid, prices, spec)
    print()
    print(report(simulation, spec, args.simples_rate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
