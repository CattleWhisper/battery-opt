r"""
Offline bill simulation: what the meter really cost, on either tariff.

Answers the switching question with measured data instead of models:
replay a Home Assistant energy-meter export against the delivered
prices the integration archived, and price the SAME kWh under both
the current Simples tariff and Indexada Horaria tri-horaria.

Deliberately reuses `core/` (ADR-0001 keeps it HA-free, which is what
makes this possible): the TAR tables and the ponta/cheias/vazio
calendar come from `core.prices` / `core.calendar`, never re-typed
here. The delivered prices come from the integration's own daily
archive, so a mismatch against `sensor.battery_opt_cost_today`'s
`variable_eur` points at the cost tracker, not at a second
implementation of the price formula.

Meter deltas follow the integration's rule exactly (cost.py, decision
8): a negative delta is a meter reset and counts as ZERO, never as
negative consumption. That makes a daily utility_meter (which resets
to 0) and a lifetime cumulative sensor both valid inputs.

Usage:
    python backtest/simulate_bill.py \\
        --meter ~/Downloads/history.csv \\
        --prices ~/homeassistant/battery_opt/prices

    # restrict to a billing window, and use the real invoice rate
    python backtest/simulate_bill.py --meter m.csv --prices p/ \\
        --from 2026-08-02 --to 2026-09-01 --simples-rate 0.1420
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.tariffs import TAR_SIMPLES_2026
from custom_components.battery_opt.core.calendar import (
    TZ_PORTUGAL,
    period,
)
from custom_components.battery_opt.core.prices import (
    K1_HORARIA,
    K2,
    K3,
    PERDAS,
    TAR_ENERGIA_2026,
    TAR_POTENCIA_2026,
)

# The invoice's own Simples TAR energia (ERSE 2026, BTN <=20.7 kVA),
# docs/tariff-reference.md §1. Simples has no period differentiation.
# Defined once, with the other options' TAR, in backtest/tariffs.py.
TAR_ENERGIA_SIMPLES = TAR_SIMPLES_2026

# Portugal: the reduced VAT rate applies to the first 200 kWh per 30
# days on contracts <= 6.9 kVA, prorated by the billing window's
# length (the 2 Aug - 1 Sep 2026 invoice split 207/526 kWh over 31
# days: 200 x 31/30 = 206.7). The rest is billed at the normal rate.
VAT_REDUCED = 0.06
VAT_NORMAL = 0.23
VAT_REDUCED_KWH_PER_30D = 200.0

_QUARTERS_PER_DAY = 96
_UTC = ZoneInfo("UTC")


@dataclass
class DayPrices:
    """One archived day: 96 delivered prices, plus the raw OMIE points."""

    delivered_eur_kwh: list[float]
    omie_eur_mwh: list[float] = field(default_factory=list)
    padded: bool = False


@dataclass
class Totals:
    """Accumulated energy and cost across the replayed window."""

    kwh: float = 0.0
    indexada_eur: float = 0.0
    kwh_by_period: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    indexada_by_period: dict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    unpriced_kwh: float = 0.0
    omie_weighted: float = 0.0  # sum(delta_kwh * OMIE), for the break-even
    omie_kwh: float = 0.0  # kWh that had an OMIE point to weight
    days: set[date] = field(default_factory=set)


def _parse_ts(raw: str) -> datetime | None:
    """Parse an ISO timestamp (HA exports UTC with a trailing Z)."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(TZ_PORTUGAL)


def load_meter(path: Path) -> list[tuple[datetime, float]]:
    """
    Read an HA history CSV into (Lisbon-local instant, reading) pairs.

    Column names are auto-detected rather than fixed: HA's own export
    is `entity_id,state,last_changed`, but the download button and the
    various history cards disagree on ordering and headers. Any row
    whose state is not a number (`unavailable`, `unknown`) is dropped
    — the same states cost.py's tracker refuses.
    """
    rows: list[tuple[datetime, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return []
        lower = [column.strip().lower() for column in header]

        def find(*names: str) -> int | None:
            for name in names:
                if name in lower:
                    return lower.index(name)
            return None

        ts_col = find("last_changed", "last_updated", "timestamp", "time", "date")
        value_col = find("state", "value", "mean", "sum")
        # No usable header: assume the shape the user already pasted,
        # `entity_id,state,timestamp`, and re-read the first row as data.
        if ts_col is None or value_col is None:
            ts_col, value_col = 2, 1
            rows.extend(_row(header, ts_col, value_col) or [])
        for record in reader:
            parsed = _row(record, ts_col, value_col)
            if parsed:
                rows.extend(parsed)
    rows.sort(key=lambda pair: pair[0])
    return rows


def _row(
    record: list[str], ts_col: int, value_col: int
) -> list[tuple[datetime, float]] | None:
    if len(record) <= max(ts_col, value_col):
        return None
    moment = _parse_ts(record[ts_col])
    if moment is None:
        return None
    try:
        value = float(record[value_col])
    except ValueError:
        return None  # unavailable / unknown / a stray header
    return [(moment, value)]


def load_prices(directory: Path) -> dict[date, DayPrices]:
    """Read the integration's daily price archive (archive.py's format)."""
    archive: dict[date, DayPrices] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        delivered = payload.get("delivered_eur_kwh")
        if not isinstance(delivered, list) or not delivered:
            continue
        try:
            day = date.fromisoformat(payload["date"])
        except KeyError, TypeError, ValueError:
            continue
        archive[day] = DayPrices(
            delivered_eur_kwh=[float(v) for v in delivered],
            omie_eur_mwh=[
                float(point["price"])
                for point in payload.get("omie_eur_mwh", [])
                if isinstance(point, dict) and "price" in point
            ],
            padded=bool(payload.get("padded")),
        )
    return archive


def quarter_index(moment: datetime) -> int:
    """Quarter-of-day for a Lisbon-local instant (core.plan convention)."""
    return min((moment.hour * 60 + moment.minute) // 15, _QUARTERS_PER_DAY - 1)


def replay(
    readings: list[tuple[datetime, float]],
    archive: dict[date, DayPrices],
    start: date | None,
    end: date | None,
) -> Totals:
    """
    Integrate meter deltas at the archived delivered price of each instant.

    Mirrors `cost.py`'s CostTracker: each consecutive pair of readings
    contributes `delta` kWh priced at the quarter containing the LATER
    reading, and a negative delta (meter reset) contributes nothing.
    Energy whose day has no archived prices is counted as energy but
    carries no cost — reported separately so a partial archive cannot
    quietly understate the total.
    """
    totals = Totals()
    for (_, previous), (moment, current) in pairwise(readings):
        day = moment.date()
        if (start and day < start) or (end and day > end):
            continue
        delta = current - previous
        if delta <= 0:  # meter reset, or no consumption
            continue
        totals.kwh += delta
        totals.days.add(day)
        totals.kwh_by_period[period(moment.replace(tzinfo=None))] += delta
        prices = archive.get(day)
        if prices is None:
            totals.unpriced_kwh += delta
            continue
        index = min(quarter_index(moment), len(prices.delivered_eur_kwh) - 1)
        cost = delta * prices.delivered_eur_kwh[index]
        totals.indexada_eur += cost
        totals.indexada_by_period[period(moment.replace(tzinfo=None))] += cost
        if index < len(prices.omie_eur_mwh):
            totals.omie_weighted += delta * prices.omie_eur_mwh[index]
            totals.omie_kwh += delta
    return totals


def vat_split(kwh: float, days: int) -> tuple[float, float]:
    """Split kWh into (reduced-rate, normal-rate) portions."""
    allowance = VAT_REDUCED_KWH_PER_30D * max(days, 1) / 30.0
    reduced = min(kwh, allowance)
    return reduced, max(0.0, kwh - reduced)


def _decomposition(
    totals: Totals,
    simples_energy: float,
    indexada_energy: float,
    tar_simples: float,
    tar_tri: float,
) -> list[str]:
    """
    Split both tariffs into commodity vs regulated TAR.

    Delivered price = OMIE/1000 x (1+PERDAS) x K1 + K2 + TAR(period).
    Everything except TAR is the "commodity" the two tariffs price
    differently; TAR is regulated and identical in kind, differing
    only in how the periods carve it up. This is the line that says
    WHICH half of the bill the switch actually moves.
    """
    commodity_indexada = indexada_energy - tar_tri
    commodity_simples = simples_energy - tar_simples
    lines = [
        "Where the difference actually comes from",
        f"  {'':<28} {'Simples':>10} {'Indexada':>10}",
        f"  {'commodity (energy+margin)':<28} "
        f"{commodity_simples:>10,.2f} {commodity_indexada:>10,.2f}",
        f"  {'TAR energia (regulated)':<28} {tar_simples:>10,.2f} {tar_tri:>10,.2f}",
    ]
    if totals.kwh:
        lines.append(
            f"  {'commodity EUR/kWh':<28} "
            f"{commodity_simples / totals.kwh:>10.4f} "
            f"{commodity_indexada / totals.kwh:>10.4f}"
        )
    lines.append("")
    return lines


def _breakeven(totals: Totals, simples_rate: float, tar_tri: float) -> list[str]:
    """
    Return the flat OMIE level at which the two tariffs cost the same.

    Solves `X/1000 x (1+PERDAS) x K1 + K2 + tar_per_kwh = simples_rate`
    for X, using THIS window's own period mix for the TAR term — so it
    answers "at what market price does switching stop paying, for my
    consumption shape", not a generic one.
    """
    if not (totals.omie_kwh and totals.kwh):
        return []
    avg_omie = totals.omie_weighted / totals.omie_kwh
    tar_per_kwh = tar_tri / totals.kwh
    slope = (1 + PERDAS) * K1_HORARIA / 1000.0
    breakeven = (simples_rate - K2 - tar_per_kwh) / slope
    lines = [
        "Break-even against the current Simples rate",
        f"  Average OMIE over the window    {avg_omie:>10,.2f} EUR/MWh",
        f"  Break-even OMIE                 {breakeven:>10,.2f} EUR/MWh",
    ]
    if avg_omie > breakeven:
        lines.append(
            f"  -> Switching LOSES money while OMIE stays above "
            f"{breakeven:,.0f} EUR/MWh."
        )
    else:
        lines.append(
            f"  -> Switching WINS while OMIE stays below {breakeven:,.0f} EUR/MWh."
        )
    lines.append("")
    return lines


def report(totals: Totals, simples_rate: float, *, with_vat: bool) -> str:
    """Render the two-tariff comparison."""
    days = len(totals.days)
    lines: list[str] = []
    add = lines.append

    add(f"Window: {days} day(s) with data, {totals.kwh:,.2f} kWh imported")
    if totals.unpriced_kwh:
        add(
            f"  WARNING: {totals.unpriced_kwh:,.2f} kWh had no archived prices "
            f"and is EXCLUDED from the Indexada cost below"
        )
    add("")

    add("Consumption by TAR period")
    add(f"  {'period':<8} {'kWh':>10} {'share':>8}")
    for name in ("vazio", "cheias", "ponta"):
        kwh = totals.kwh_by_period.get(name, 0.0)
        share = kwh / totals.kwh * 100 if totals.kwh else 0.0
        add(f"  {name:<8} {kwh:>10,.2f} {share:>7.1f}%")
    add("")

    # --- energy component, excl. VAT and fixed terms ---
    simples_energy = totals.kwh * simples_rate
    indexada_energy = totals.indexada_eur
    add("Energy component (excl. VAT, excl. fixed terms)")
    add(f"  Simples @ {simples_rate:.4f} EUR/kWh   {simples_energy:>10,.2f} EUR")
    add(f"  Indexada Horaria (measured)     {indexada_energy:>10,.2f} EUR")
    if totals.kwh:
        add(
            f"  Indexada effective rate         "
            f"{indexada_energy / totals.kwh:>10.4f} EUR/kWh"
        )
    delta = indexada_energy - simples_energy
    verdict = "MORE expensive" if delta > 0 else "CHEAPER"
    add(f"  Difference                      {delta:>+10,.2f} EUR  ({verdict})")
    add("")

    # --- the TAR-only view: the project's actual thesis ---
    tar_simples = totals.kwh * TAR_ENERGIA_SIMPLES
    tar_tri = sum(
        totals.kwh_by_period.get(name, 0.0) * TAR_ENERGIA_2026[name]
        for name in ("vazio", "cheias", "ponta")
    )
    add("TAR energia only (the part switching tariff actually changes)")
    add(f"  Simples    @ {TAR_ENERGIA_SIMPLES:.4f}          {tar_simples:>10,.2f} EUR")
    add(f"  Tri-horaria (per period)        {tar_tri:>10,.2f} EUR")
    add(f"  Difference                      {tar_tri - tar_simples:>+10,.2f} EUR")
    add("")

    lines.extend(
        _decomposition(totals, simples_energy, indexada_energy, tar_simples, tar_tri)
    )
    lines.extend(_breakeven(totals, simples_rate, tar_tri))

    if with_vat and days:
        fixed = (K3 + TAR_POTENCIA_2026) * days
        add(f"Full bill estimate ({days} days, incl. VAT and fixed terms)")
        add(f"  Fixed terms (K3 + TAR potencia) {fixed:>10,.2f} EUR")
        for label, energy in (
            ("Simples", simples_energy),
            ("Indexada", indexada_energy),
        ):
            reduced_kwh, _ = vat_split(totals.kwh, days)
            share = reduced_kwh / totals.kwh if totals.kwh else 0.0
            billed = (
                energy * share * (1 + VAT_REDUCED)
                + energy * (1 - share) * (1 + VAT_NORMAL)
                + fixed * (1 + VAT_NORMAL)
            )
            add(f"  {label:<8} total (incl. VAT)      {billed:>10,.2f} EUR")
        add("")
        add(
            "  NOTE: VAT split assumes the reduced rate on the first "
            f"{VAT_REDUCED_KWH_PER_30D:.0f} kWh/30 days;"
        )
        add("        the commercial margin is assumed equal on both tariffs.")
    return "\n".join(lines)


def main() -> int:
    """Parse arguments, replay the meter, print the comparison."""
    parser = argparse.ArgumentParser(
        description="Compare Simples vs Indexada Horaria on measured meter data.",
    )
    parser.add_argument("--meter", required=True, type=Path, help="HA history CSV")
    parser.add_argument(
        "--prices", required=True, type=Path, help="battery_opt/prices archive dir"
    )
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--simples-rate",
        type=float,
        default=0.1420,
        help="EUR/kWh actually paid, post-discount (invoice: 0.1671 x 0.85)",
    )
    parser.add_argument(
        "--no-vat", action="store_true", help="skip the full-bill VAT estimate"
    )
    args = parser.parse_args()

    readings = load_meter(args.meter)
    if len(readings) < 2:
        print(f"Not enough usable rows in {args.meter}", file=sys.stderr)
        return 1
    archive = load_prices(args.prices)
    if not archive:
        print(f"No price archive files found in {args.prices}", file=sys.stderr)
        return 1

    print(
        f"Loaded {len(readings):,} meter rows "
        f"({readings[0][0].date()} .. {readings[-1][0].date()}) "
        f"and {len(archive)} archived price day(s)\n"
    )
    totals = replay(readings, archive, args.start, args.end)
    if not totals.kwh:
        print("No consumption in the selected window.", file=sys.stderr)
        return 1
    print(report(totals, args.simples_rate, with_vat=not args.no_vat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
