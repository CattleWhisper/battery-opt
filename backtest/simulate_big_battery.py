r"""
What-if: a large battery charged only in each day's cheapest quarters.

A sizing/strategy sketch, deliberately simpler than `core/optimiser.py`
— no wear cost, no ponta targeting, no validator. The question is "how
big a bill would a big battery leave", not "what would the planner do".

The day is walked quarter by quarter and every kWh is priced where it
actually falls; there is no assumed average anywhere. Quarters split
into two kinds, chosen by price:

- **cheap** — the house draws from the grid AND the battery charges
  into whatever contracted-power headroom is left. Serving the load
  from the grid here rather than from the pack is deliberate: it keeps
  the stored energy for the dear quarters, which is the arbitrage.
- **everything else** — the battery covers the load, and only the
  shortfall it cannot meet is imported, at that quarter's own price.

Prices come from OMIE through the project's own delivered-price
formula (`core.prices.price`), so they are the same EUR/kWh the
integration would compute. Days missing from the local OMIE cache are
downloaded on demand (`backtest/download_omie.py`).

The same consumption is then priced under every tariff in the
parameters file (`backtest/tariffs.toml`, `--tariffs`) and the battery
is simulated against each; the ranking is the whole bill excl. VAT —
energy plus each tariff's daily fixed term. The strategy is price-driven,
so it adapts per tariff on its own: on a flat tariff the
profitability guard never lets it charge, and the battery column
equals the no-battery one.

Three things keep the answer honest rather than a pure price sort:

- **Round-trip efficiency.** Delivering X kWh from the battery costs
  X / eta at the meter, so total import always EXCEEDS consumption.
- **Contracted power.** Charging cannot exceed `--p-usable` minus the
  house load already drawn in that quarter, so the cheapest quarters
  have finite room and a big battery may simply not fit its charge
  into one night.
- **Causality.** The walk is chronological and the state of charge
  chains across midnight (as `backtest/run.py` does), so energy cannot
  be spent before it is bought, and a cheap late-evening quarter is
  not silently discarded at the day boundary.

Consumption comes from either a real meter export (`--meter`) or a
flat daily figure (`--daily-kwh`), so the tool can answer a sizing
question before any measurement exists.

Usage:
    python backtest/simulate_big_battery.py --meter ~/Downloads/history.csv \
        --capacity 20 --from 2026-08-31 --to 2026-09-08

    python backtest/simulate_big_battery.py --daily-kwh 23.6 \
        --capacity 20 --from 2026-08-31 --to 2026-09-08

    # only some of the tariffs (names as in the parameters file)
    python backtest/simulate_big_battery.py --daily-kwh 23.6 \
        --from 2026-08-31 --to 2026-09-08 \
        --only "dinamica tri-horaria semanal" "dinamica bi-horaria semanal"

    # a different parameters file (default: backtest/tariffs.toml)
    python backtest/simulate_big_battery.py --daily-kwh 23.6 \
        --from 2026-08-31 --to 2026-09-08 --tariffs my-offers.toml
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.download_omie import DATA_DIR, download
from backtest.load_omie import load_series
from backtest.simulate_bill import load_meter, quarter_index
from backtest.tariffs import (
    DEFAULT_TARIFFS_FILE,
    Tariff,
    TariffFileError,
    load_tariffs,
)

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
    eta_roundtrip: float = DEFAULT_ETA_ROUNDTRIP
    p_usable_w: float = DEFAULT_P_USABLE_W


@dataclass
class DayResult:
    """One simulated day, every kWh priced at the quarter it was drawn in."""

    day: date
    consumption_kwh: float = 0.0
    grid_kwh: float = 0.0  # everything crossing the meter, load + charging
    cost_eur: float = 0.0  # that import at each quarter's own price
    charged_kwh: float = 0.0  # grid-side energy put into the battery
    discharged_kwh: float = 0.0  # battery-side energy delivered to the load
    uncovered_kwh: float = 0.0  # load the battery could not serve
    baseline_eur: float = 0.0  # same consumption, no battery, spot prices
    soc_start_kwh: float = 0.0
    soc_end_kwh: float = 0.0
    charge_quarters: int = 0

    @property
    def total_eur(self) -> float:
        """Simulated cost for the day."""
        return self.cost_eur

    @property
    def avg_import_price(self) -> float:
        """Mean EUR/kWh actually paid across everything imported."""
        return self.cost_eur / self.grid_kwh if self.grid_kwh else 0.0


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


def flat_consumption(
    daily_kwh: float, start: date, end: date
) -> dict[date, list[float]]:
    """
    Spread one daily figure evenly over every day in [start, end].

    The alternative to a meter export, for sizing questions asked
    before any measurement exists ("what would 20 kWh do at ~24
    kWh/day?"). An even split is the project's own load model rather
    than an arbitrary choice: CONTEXT.md runs a flat 1.04 kW BASE_LOAD,
    and the June invoice confirmed the profile is "effectively flat".

    What it cannot show is a consumption SHAPE effect. With a flat
    load every quarter offers identical charging headroom, so the
    cheap-window search is decided purely by price — a real profile
    that peaks in the expensive hours would do worse than this says.
    """
    per_quarter = daily_kwh / QUARTERS_PER_DAY
    grid: dict[date, list[float]] = {}
    day = start
    while day <= end:
        grid[day] = [per_quarter] * QUARTERS_PER_DAY
        day += timedelta(days=1)
    return grid


def omie_by_quarter(
    days: list[date], omie_dir: Path, *, allow_download: bool
) -> dict[date, list[float | None]]:
    """
    Build 96 raw OMIE prices (EUR/MWh) per Lisbon day from the cache.

    Raw rather than delivered: every tariff turns the SAME OMIE quarter
    into its own EUR/kWh (`tariff_prices`), so the market data is read
    once and no tariff can end up priced on different quarters.

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

    omie: dict[date, list[float | None]] = {}
    for record in records:
        local = record.start
        day = local.date()
        if day not in omie:
            omie[day] = [None] * QUARTERS_PER_DAY
        index = quarter_index(local)
        # marginalpdbc is hourly before Oct 2025 and quarter-hourly
        # after; an hourly record fills its four quarters.
        span = max(1, round(record.duration_hours / QUARTER_HOURS))
        for offset in range(span):
            if index + offset < QUARTERS_PER_DAY:
                omie[day][index + offset] = record.price_eur_mwh
    return omie


def tariff_prices(
    omie: dict[date, list[float | None]], tariff: Tariff
) -> dict[date, list[float | None]]:
    """
    Price every OMIE quarter under one tariff (EUR/kWh, None where unpriced).

    Quarter q of a Lisbon day starts at local midnight + 15q minutes, in
    naive Portugal legal time — the calendar API contract.
    """
    prices: dict[date, list[float | None]] = {}
    for day, quarters in omie.items():
        midnight = datetime(day.year, day.month, day.day)
        prices[day] = [
            tariff.quarter_price(value, midnight + timedelta(minutes=15 * q))
            for q, value in enumerate(quarters)
        ]
    return prices


def quarter_headroom_kwh(load_kwh: float, spec: BatterySpec) -> float:
    """Charging room left in a quarter after its own house load."""
    load_w = load_kwh / QUARTER_HOURS * 1000.0
    return max(0.0, spec.p_usable_w - load_w) * QUARTER_HOURS / 1000.0


def dischargeable_load_kwh(
    load_kwh: list[float],
    day_prices: list[float | None],
    spec: BatterySpec,
) -> float:
    """
    Load sitting in quarters dear enough to be worth serving from the pack.

    A quarter is worth covering only if its price beats the cheapest
    quarter's price grossed up for round-trip losses. Summing that load
    gives the day's real storage NEED, which is what the charge window
    should aim at — not the pack's nameplate.
    """
    priced = [q for q in range(len(day_prices)) if day_prices[q] is not None]
    if not priced:
        return 0.0
    threshold = min(day_prices[q] for q in priced) / spec.eta_roundtrip
    return sum(load_kwh[q] for q in priced if day_prices[q] > threshold)


def select_charge_quarters(
    load_kwh: list[float],
    day_prices: list[float | None],
    spec: BatterySpec,
    soc_start_kwh: float = 0.0,
) -> set[int]:
    """
    Pick the cheapest quarters worth charging in, enough to fill the pack.

    Two stopping conditions, and the second one matters: charging stops
    when the pack's target is met OR when the candidate is no longer
    cheap enough to pay for itself. A kWh bought at `p` and delivered
    later displaces one bought at `q`, and only pays if
    `q > p / eta_roundtrip` — the same condition `core/optimiser.py`
    calls C-8, minus the wear term this sketch deliberately omits.

    Without that guard a large pack keeps buying into ever dearer
    quarters simply because it has room, and the simulation reports a
    BIGGER battery as more expensive — an artifact of the strategy,
    not a property of the battery.
    """
    priced = [q for q in range(len(day_prices)) if day_prices[q] is not None]
    if not priced:
        return set()
    dearest = max(day_prices[q] for q in priced)
    eta_one_way = spec.eta_roundtrip**0.5
    # Buy the lesser of what the day NEEDS and what the pack can still
    # hold. Targeting the nameplate instead makes an oversized pack
    # buy energy the window never uses, which reports a bigger battery
    # as more expensive for no physical reason.
    need_kwh = dischargeable_load_kwh(load_kwh, day_prices, spec)
    room_kwh = max(0.0, spec.capacity_kwh - soc_start_kwh)
    target = min(need_kwh / spec.eta_roundtrip, room_kwh / eta_one_way)
    chosen: set[int] = set()
    remaining = target
    for quarter in sorted(priced, key=lambda q: day_prices[q]):
        if remaining <= 1e-9:
            break
        if day_prices[quarter] / spec.eta_roundtrip >= dearest:
            break  # cannot pay for itself even against the day's peak
        headroom = quarter_headroom_kwh(load_kwh[quarter], spec)
        if headroom <= 0:
            continue
        chosen.add(quarter)
        remaining -= headroom
    return chosen


def select_discharge_quarters(
    load_kwh: list[float],
    day_prices: list[float | None],
    charge_quarters: set[int],
    available_kwh: float,
) -> set[int]:
    """
    Pick the DEAREST quarters to spend the pack on; the rest just hold.

    The missing third state. "Charge in the cheapest, discharge in
    everything else" quietly empties the pack into whatever comes next
    chronologically — which, on a day whose cheap block is longer than
    the charge window, means discharging into cheap quarters and
    arriving at the expensive ones empty. Selecting discharge targets
    dearest-first instead makes HOLD the default, which is what the
    real executor does (ADR-0006's three states).

    `available_kwh` is meter-side energy the pack can deliver, so the
    selection stops once the day's stored charge is spoken for.
    """
    priced = [
        q
        for q in range(len(day_prices))
        if day_prices[q] is not None and q not in charge_quarters
    ]
    chosen: set[int] = set()
    remaining = available_kwh
    for quarter in sorted(priced, key=lambda q: day_prices[q], reverse=True):
        if remaining <= 1e-9:
            break
        chosen.add(quarter)
        remaining -= load_kwh[quarter]
    return chosen


def simulate_day(
    day: date,
    load_kwh: list[float],
    day_prices: list[float | None],
    spec: BatterySpec,
    soc_start_kwh: float = 0.0,
) -> DayResult:
    """
    Walk the day quarter by quarter, pricing every kWh where it falls.

    Three states, as the real executor has (ADR-0006):

    - **CHARGE** (the cheapest quarters): the house draws from the grid
      AND the battery charges into whatever contracted-power headroom
      is left. Serving the load from the grid here is deliberate — it
      keeps the stored energy for the dear quarters.
    - **DISCHARGE** (the dearest quarters the stored energy can reach):
      the battery covers the load, and only the shortfall is imported.
    - **HOLD** (everything else): the pack sits still and the load is
      simply bought. This is the state that makes the arbitrage work;
      without it the pack drains into the first quarters that follow
      the charge window, cheap or not, and arrives at the peak empty.

    Every import is charged at that quarter's own delivered price, so
    nothing rests on an assumed average. Unpriced quarters (a gap in
    the OMIE publication) are still simulated physically but contribute
    no cost, mirroring the integration's own cost tracker.
    """
    result = DayResult(day=day, soc_start_kwh=soc_start_kwh)
    result.consumption_kwh = sum(load_kwh)
    result.baseline_eur = sum(
        kwh * day_prices[q]
        for q, kwh in enumerate(load_kwh)
        if day_prices[q] is not None
    )
    eta_one_way = spec.eta_roundtrip**0.5
    charge_quarters = select_charge_quarters(load_kwh, day_prices, spec, soc_start_kwh)
    # What the pack will be able to deliver today: what it already
    # holds, plus what the charge window is about to put in.
    incoming = sum(
        min(
            quarter_headroom_kwh(load_kwh[q], spec),
            max(0.0, spec.capacity_kwh) / eta_one_way,
        )
        for q in charge_quarters
    )
    available = min(spec.capacity_kwh, soc_start_kwh + incoming * eta_one_way)
    discharge_quarters = select_discharge_quarters(
        load_kwh, day_prices, charge_quarters, available * eta_one_way
    )
    result.charge_quarters = len(charge_quarters)

    soc = soc_start_kwh
    for quarter, load in enumerate(load_kwh):
        price_eur_kwh = day_prices[quarter] if quarter < len(day_prices) else None
        if quarter in charge_quarters:
            room_kwh = max(0.0, spec.capacity_kwh - soc)
            take = min(quarter_headroom_kwh(load, spec), room_kwh / eta_one_way)
            soc += take * eta_one_way
            imported = load + take
            result.charged_kwh += take
        elif quarter in discharge_quarters:
            served = min(load, soc * eta_one_way)
            soc -= served / eta_one_way
            imported = load - served
            result.discharged_kwh += served
            result.uncovered_kwh += imported
        else:  # HOLD: the pack sits still, the load is bought
            imported = load
        result.grid_kwh += imported
        if price_eur_kwh is not None:
            result.cost_eur += imported * price_eur_kwh

    result.soc_end_kwh = soc
    return result


def run(
    grid: dict[date, list[float]],
    prices: dict[date, list[float | None]],
    spec: BatterySpec,
    soc_start_kwh: float = 0.0,
) -> Simulation:
    """
    Simulate every day that has prices, chaining the state of charge.

    Days are walked in order and the pack carries over midnight, as
    `backtest/run.py` does: without chaining, energy bought in a cheap
    late-evening quarter would simply vanish at midnight and the answer
    would depend on where the window happened to start. A day with no
    prices breaks the chain rather than guessing through it.
    """
    simulation = Simulation()
    soc = soc_start_kwh
    for day in sorted(grid):
        day_prices = prices.get(day)
        if day_prices is None or all(value is None for value in day_prices):
            simulation.missing_prices.add(day)
            continue
        result = simulate_day(day, grid[day], day_prices, spec, soc)
        soc = result.soc_end_kwh
        simulation.days.append(result)
    return simulation


def by_month(days: list[DayResult]) -> dict[str, list[DayResult]]:
    """Group simulated days into calendar months, in order."""
    months: dict[str, list[DayResult]] = defaultdict(list)
    for day in days:
        months[day.day.strftime("%Y-%m")].append(day)
    return dict(sorted(months.items()))


def _monthly_lines(simulation: Simulation, reference_rate: float) -> list[str]:
    """
    Render the per-month rollup; "ref" is the reference tariff's energy.

    Calendar months, which is NOT the billing period: EDP invoices run
    2nd-to-1st, so a month here will not tie out exactly against a
    bill. Use `--from`/`--to` to match an invoice window when that is
    what you need to reconcile.
    """
    months = by_month(simulation.days)
    if not months:
        return []
    lines = [
        "Monthly summary",
        f"  {'month':<10}{'days':>6}{'used':>10}{'import':>10}"
        f"{'EUR/kWh':>9}{'sim EUR':>10}{'spot EUR':>10}"
        f"{'ref EUR':>10}{'vs ref':>12}",
    ]
    for month, days in months.items():
        used = sum(day.consumption_kwh for day in days)
        imported = sum(day.grid_kwh for day in days)
        sim = sum(day.cost_eur for day in days)
        spot = sum(day.baseline_eur for day in days)
        ref = used * reference_rate
        rate = sim / used if used else 0.0
        lines.append(
            f"  {month:<10}{len(days):>6}{used:>10.1f}{imported:>10.1f}"
            f"{rate:>9.4f}{sim:>10.2f}{spot:>10.2f}"
            f"{ref:>10.2f}{sim - ref:>+12.2f}"
        )
    lines.append("")
    return lines


def report(
    simulation: Simulation,
    spec: BatterySpec,
    reference_rate: float,
    *,
    daily: bool = True,
    reference_label: str = "Simples",
) -> str:
    """
    Render the per-day table, the monthly rollup and the window summary.

    ENERGY only, excl. VAT and fixed terms: this report is about what
    the battery does on one tariff. `reference_rate` is the reference
    tariff's effective EUR/kWh; the whole-bill comparison across
    tariffs is `comparison_lines`.
    """
    lines: list[str] = []
    add = lines.append
    if simulation.missing_prices:
        add(
            f"WARNING: no prices for {len(simulation.missing_prices)} day(s): "
            + ", ".join(str(d) for d in sorted(simulation.missing_prices))
        )
        add("")

    if daily:
        add(f"Per-day simulation ({spec.capacity_kwh:g} kWh battery, SoC chained)")
        add(
            f"  {'day':<12}{'used':>8}{'import':>9}{'from batt':>11}"
            f"{'uncov':>8}{'SoC end':>9}{'EUR/kWh':>9}{'sim EUR':>9}{'spot EUR':>10}"
        )
        for day in simulation.days:
            add(
                f"  {day.day!s:<12}{day.consumption_kwh:>8.2f}{day.grid_kwh:>9.2f}"
                f"{day.discharged_kwh:>11.2f}{day.uncovered_kwh:>8.2f}"
                f"{day.soc_end_kwh:>9.2f}{day.avg_import_price:>9.4f}"
                f"{day.total_eur:>9.2f}{day.baseline_eur:>10.2f}"
            )
        add("")
    lines.extend(_monthly_lines(simulation, reference_rate))

    n = len(simulation.days)
    if not n:
        return "\n".join(lines)
    kwh = simulation.total("consumption_kwh")
    imported = simulation.total("grid_kwh")
    sim = simulation.total("cost_eur")
    spot = simulation.total("baseline_eur")
    ref = kwh * reference_rate
    ref_name = f"{reference_label} @ {reference_rate:.4f}"

    add(f"Window totals ({n} day(s), {kwh:,.1f} kWh consumed; energy only)")
    add(
        f"  {'Simulated (with battery)':<32}{sim:>10,.2f} EUR   "
        f"{sim / kwh:>7.4f} EUR/kWh consumed"
    )
    add(
        f"  {'Same tariff, no battery':<32}{spot:>10,.2f} EUR   "
        f"{spot / kwh:>7.4f} EUR/kWh consumed"
    )
    add(
        f"  {ref_name[:32]:<32}{ref:>10,.2f} EUR   "
        f"{reference_rate:>7.4f} EUR/kWh consumed"
    )
    add("")
    add(f"  vs no battery : {sim - spot:>+10,.2f} EUR over the window")
    add(f"  vs reference  : {sim - ref:>+10,.2f} EUR over the window")
    add(f"  annualised difference vs reference: {(sim - ref) / n * 365:>+,.0f} EUR/yr")
    add("")
    served = simulation.total("discharged_kwh")
    # Import EXCEEDS consumption by the round-trip losses: every kWh
    # cycled through the pack is bought at 1/eta.
    add(
        f"  imported {imported:,.1f} kWh to consume {kwh:,.1f} kWh "
        f"({imported - kwh:+,.1f} kWh of round-trip loss)"
    )
    add(
        f"  battery covered {served / kwh * 100:.1f}% of consumption; "
        f"{simulation.total('uncovered_kwh'):,.1f} kWh came from the grid "
        f"at the going rate"
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class TariffRun:
    """One tariff's simulation over the shared consumption window."""

    tariff: Tariff
    simulation: Simulation

    def energy_eur(self, *, battery: bool) -> float:
        """Energy cost over the window, with or without the battery."""
        return self.simulation.total("cost_eur" if battery else "baseline_eur")

    @property
    def fixed_eur(self) -> float:
        """The tariff's daily fixed term over every simulated day."""
        return self.tariff.daily_fixed_eur * len(self.simulation.days)

    def total_eur(self, *, battery: bool) -> float:
        """Return the bill excl. VAT: energy plus fixed terms."""
        return self.energy_eur(battery=battery) + self.fixed_eur


def compare(
    grid: dict[date, list[float]],
    omie: dict[date, list[float | None]],
    tariffs: list[Tariff],
    spec: BatterySpec,
) -> list[TariffRun]:
    """
    Simulate the same consumption and battery under every tariff.

    All tariffs price the same OMIE quarters (`Tariff.quarter_price`
    returns None wherever OMIE is missing, fixed offers included), so
    every run covers the same days and the totals are comparable.
    """
    return [
        TariffRun(tariff, run(grid, tariff_prices(omie, tariff), spec))
        for tariff in tariffs
    ]


def _money(value: float) -> float:
    """
    Round to cents without printing "-0.00".

    `sum()` uses compensated summation while the day walk accumulates
    with `+=`, so two costs that are equal in every quarter can differ
    by ~1e-15 and format as a negative zero — which on a flat tariff
    reads as "the battery lost money".
    """
    return round(value, 2) + 0.0


def comparison_lines(
    runs: list[TariffRun], spec: BatterySpec, reference: str
) -> list[str]:
    """
    Render the ranking: cheapest bill with the battery first.

    The bill is energy plus the daily fixed term, excl. VAT. Ranking on
    energy alone would favour any offer that moves its margin from the
    kWh price into the fixed term; `vs` and `/yr` compare that whole
    bill against the `reference` tariff (by name).
    """
    if not runs or not runs[0].simulation.days:
        return []
    ref_run = next(r for r in runs if r.tariff.name == reference)
    ref_total = ref_run.total_eur(battery=True)
    n = len(runs[0].simulation.days)
    kwh = runs[0].simulation.total("consumption_kwh")
    name_width = max(30, *(len(r.tariff.name) + 2 for r in runs))
    lines = [
        f"Tariff comparison ({n} day(s), {kwh:,.1f} kWh consumed, "
        f"{spec.capacity_kwh:g} kWh battery; EUR excl. VAT)",
        f"  {'':<{name_width}}{'--- energy ---':^26}{'--- bill, with battery ---':^46}",
        f"  {'tariff':<{name_width}}{'no batt':>9}{'w/ batt':>9}{'saved':>8}"
        f"{'fixed':>8}{'total':>9}{'EUR/kWh':>9}{'vs ref':>10}{'/yr':>10}",
    ]
    for tariff_run in sorted(runs, key=lambda r: r.total_eur(battery=True)):
        without = tariff_run.energy_eur(battery=False)
        with_battery = tariff_run.energy_eur(battery=True)
        total = tariff_run.total_eur(battery=True)
        delta = total - ref_total
        marker = "  <- ref" if tariff_run is ref_run else ""
        lines.append(
            f"  {tariff_run.tariff.name:<{name_width}}{without:>9.2f}"
            f"{with_battery:>9.2f}{_money(without - with_battery):>8.2f}"
            f"{tariff_run.fixed_eur:>8.2f}{total:>9.2f}{total / kwh:>9.4f}"
            f"{_money(delta):>+10.2f}{round(delta / n * 365) + 0:>+10d}{marker}"
        )
    lines += [
        "",
        f"  ref = {reference}. fixed = each tariff's daily fixed term over the",
        "  window; EUR/kWh = the whole bill per kWh consumed.",
        "",
    ]
    return lines


def monthly_comparison_lines(runs: list[TariffRun]) -> list[str]:
    """
    Render the bill with the battery (energy + fixed), tariff by month.

    One row per tariff, in the ranking's order, one column per month: a
    parameters file can hold dozens of tariffs but a window only a few
    months, so this shape stays readable where a column per tariff did
    not. Calendar months — not EDP's 2nd-to-1st billing window (see
    `_monthly_lines`) — so use --from/--to to reconcile an invoice.
    """
    if not runs or not runs[0].simulation.days:
        return []
    ranked = sorted(runs, key=lambda r: r.total_eur(battery=True))
    months = list(by_month(runs[0].simulation.days))
    name_width = max(30, *(len(r.tariff.name) + 2 for r in ranked))
    lines = [
        "Monthly bill with battery, by tariff (energy + fixed, EUR excl. VAT)",
        f"  {'tariff':<{name_width}}"
        + "".join(f"{month:>10}" for month in months)
        + f"{'total':>10}",
    ]
    for tariff_run in ranked:
        by_month_days = by_month(tariff_run.simulation.days)
        cells = ""
        for month in months:
            days = by_month_days.get(month, [])
            bill = sum(d.cost_eur for d in days)
            bill += tariff_run.tariff.daily_fixed_eur * len(days)
            cells += f"{bill:>10.2f}"
        total = tariff_run.total_eur(battery=True)
        lines.append(f"  {tariff_run.tariff.name:<{name_width}}{cells}{total:>10.2f}")
    lines.append("")
    return lines


def _synthetic_grid(args: argparse.Namespace) -> dict[date, list[float]] | None:
    """Build a flat window from --daily-kwh, or explain why it cannot."""
    if args.start is None or args.end is None:
        print("--daily-kwh needs both --from and --to", file=sys.stderr)
        return None
    if args.end < args.start:
        print("--to is before --from", file=sys.stderr)
        return None
    if args.daily_kwh <= 0:
        print("--daily-kwh must be positive", file=sys.stderr)
        return None
    grid = flat_consumption(args.daily_kwh, args.start, args.end)
    print(
        f"Synthetic load: {args.daily_kwh:g} kWh/day flat, "
        f"{len(grid)} day(s) ({min(grid)} .. {max(grid)})"
    )
    return grid


def _meter_grid(args: argparse.Namespace) -> dict[date, list[float]] | None:
    """Build the window from a meter export, clipped to --from/--to."""
    readings = load_meter(args.meter)
    if len(readings) < 2:
        print(f"Not enough usable rows in {args.meter}", file=sys.stderr)
        return None
    grid = consumption_by_quarter(readings)
    if args.start:
        grid = {d: v for d, v in grid.items() if d >= args.start}
    if args.end:
        grid = {d: v for d, v in grid.items() if d <= args.end}
    if not grid:
        print("No consumption in the selected window.", file=sys.stderr)
        return None
    print(
        f"Meter: {len(readings):,} rows, {len(grid)} day(s) "
        f"({min(grid)} .. {max(grid)})"
    )
    return grid


def build_grid(args: argparse.Namespace) -> dict[date, list[float]] | None:
    """Resolve consumption from whichever source the caller chose."""
    if args.daily_kwh is not None:
        return _synthetic_grid(args)
    return _meter_grid(args)


def main() -> int:
    """Parse arguments, simulate, print."""
    parser = argparse.ArgumentParser(
        description="Simulate a large battery charged in each day's cheapest hours.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--meter", type=Path, help="HA energy-meter history CSV")
    source.add_argument(
        "--daily-kwh",
        type=float,
        help="flat daily consumption instead of a meter export; needs --from and --to",
    )
    parser.add_argument("--capacity", type=float, default=20.0, help="kWh usable")
    parser.add_argument("--eta", type=float, default=DEFAULT_ETA_ROUNDTRIP)
    parser.add_argument("--p-usable", type=float, default=DEFAULT_P_USABLE_W)
    parser.add_argument(
        "--tariffs",
        type=Path,
        default=DEFAULT_TARIFFS_FILE,
        help="tariff parameters file (default: backtest/tariffs.toml)",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        default=None,
        help=(
            "compare only these tariffs, by their name in the parameters file "
            '(quote names with spaces: --only "fixa simples" '
            '"dinamica tri-horaria semanal"); tariffs left out may still hold TODOs'
        ),
    )
    parser.add_argument("--omie-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--no-download", action="store_true", help="never fetch missing OMIE days"
    )
    parser.add_argument(
        "--no-daily",
        action="store_true",
        help="skip the per-day table and show only the monthly rollup",
    )
    args = parser.parse_args()

    # Before any download: a file with TODOs should fail in a second,
    # not after fetching a month of OMIE.
    try:
        parameters = load_tariffs(args.tariffs, only=args.only)
    except TariffFileError as err:
        print(err, file=sys.stderr)
        return 2

    grid = build_grid(args)
    if grid is None:
        return 1
    omie = omie_by_quarter(
        sorted(grid), args.omie_dir, allow_download=not args.no_download
    )
    if not omie:
        print("No prices available; cannot simulate.", file=sys.stderr)
        return 1

    spec = BatterySpec(
        capacity_kwh=args.capacity,
        eta_roundtrip=args.eta,
        p_usable_w=args.p_usable,
    )
    for note in parameters.notes:
        print(f"Note: {note}")
    runs = compare(grid, omie, parameters.tariffs, spec)
    by_name = {r.tariff.name: r for r in runs}
    if parameters.detailed is not None:
        reference = by_name[parameters.reference]
        detailed = by_name[parameters.detailed]
        kwh = reference.simulation.total("consumption_kwh")
        reference_rate = reference.energy_eur(battery=True) / kwh if kwh else 0.0
        print()
        print(f"Detailed battery report: {parameters.detailed}")
        print(
            report(
                detailed.simulation,
                spec,
                reference_rate,
                daily=not args.no_daily,
                reference_label=parameters.reference,
            )
        )
    print()
    print("\n".join(comparison_lines(runs, spec, parameters.reference)))
    print("\n".join(monthly_comparison_lines(runs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
