"""
Tests for the large-battery what-if (backtest/simulate_big_battery.py).

The tool walks the day quarter by quarter and prices every kWh where
it falls, so the properties worth pinning are physical rather than
arithmetic: energy is conserved, the pack never exceeds its capacity
or goes negative, the contracted-power ceiling holds in every quarter,
and the state of charge chains across midnight. Those are what make
the answer non-linear in capacity — and what a flat "excess price"
assumption used to paper over.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backtest.simulate_big_battery import (
    QUARTER_HOURS,
    QUARTERS_PER_DAY,
    BatterySpec,
    billing_window_start,
    by_month,
    compare,
    comparison_lines,
    flat_consumption,
    monthly_comparison_lines,
    report,
    run,
    select_charge_quarters,
    simulate_day,
    tariff_prices,
)
from backtest.tariffs import bi_table, dynamic, fixed, flat_table, tri_table

DAY = date(2026, 9, 2)
CHEAP, DEAR = 0.05, 0.40
SPEC = BatterySpec(capacity_kwh=20.0)


def _prices(cheap_quarters: int) -> list[float]:
    """Return a price vector: the first N quarters cheap, the rest dear."""
    return [CHEAP if q < cheap_quarters else DEAR for q in range(QUARTERS_PER_DAY)]


def _flat_load(total_kwh: float) -> list[float]:
    return [total_kwh / QUARTERS_PER_DAY] * QUARTERS_PER_DAY


def test_energy_is_conserved() -> None:
    """
    Import equals the load bought directly plus everything stored.

    The accounting identity the whole report rests on: nothing may be
    consumed that was not either imported at the time or discharged
    from the pack.
    """
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    direct = result.consumption_kwh - result.discharged_kwh
    assert result.grid_kwh == pytest.approx(direct + result.charged_kwh)


def test_import_exceeds_consumption_by_the_round_trip_loss() -> None:
    """Cycling a kWh through the pack costs more than not cycling it."""
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    assert result.charged_kwh > 0
    assert result.grid_kwh > result.consumption_kwh


def test_the_pack_never_exceeds_capacity_or_goes_negative() -> None:
    """State of charge stays inside [0, capacity] all day."""
    spec = BatterySpec(capacity_kwh=5.0)
    soc = 0.0
    for _ in range(3):  # several chained days, so it reaches steady state
        result = simulate_day(DAY, _flat_load(24.0), _prices(48), spec, soc)
        soc = result.soc_end_kwh
        assert -1e-9 <= soc <= spec.capacity_kwh + 1e-9


def test_the_contracted_power_ceiling_holds_in_every_quarter() -> None:
    """
    Load plus charging never exceeds --p-usable.

    This is the invariant that stops the simulation from "solving" a
    big pack by drawing power the supply cannot deliver.
    """
    spec = BatterySpec(capacity_kwh=50.0, p_usable_w=2000.0)
    load = _flat_load(24.0)  # 1 kW flat
    charge_quarters = select_charge_quarters(load, _prices(96), spec)
    for quarter in charge_quarters:
        load_w = load[quarter] / QUARTER_HOURS * 1000.0
        assert load_w <= spec.p_usable_w
    # 1 kW load under a 2 kW ceiling leaves 1 kW of charging room.
    result = simulate_day(DAY, load, _prices(96), spec)
    max_quarter_import = spec.p_usable_w * QUARTER_HOURS / 1000.0
    assert result.charged_kwh <= max_quarter_import * len(charge_quarters) + 1e-9


def test_charging_happens_in_the_cheapest_quarters() -> None:
    """Selection is by price, not by clock."""
    chosen = select_charge_quarters(_flat_load(24.0), _prices(32), SPEC)
    assert chosen  # something was picked
    assert all(q < 32 for q in chosen)  # all inside the cheap block


def test_load_in_a_cheap_quarter_is_bought_not_discharged() -> None:
    """
    Deliberate: cheap quarters are served from the grid.

    Spending the pack during the cheapest hours would throw away the
    arbitrage, so the battery is held for the dear ones.
    """
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), SPEC)
    non_charge = QUARTERS_PER_DAY - result.charge_quarters
    # Everything the battery served came from non-charge quarters.
    assert result.discharged_kwh <= non_charge * (24.0 / QUARTERS_PER_DAY) + 1e-9


def test_every_kwh_is_priced_at_its_own_quarter() -> None:
    """
    No flat rate anywhere: an all-cheap day costs exactly the cheap price.

    With one price all day the effective rate must equal it, which it
    cannot if any part of the bill is charged at an assumed average.
    """
    result = simulate_day(DAY, _flat_load(24.0), [CHEAP] * QUARTERS_PER_DAY, SPEC)
    assert result.avg_import_price == pytest.approx(CHEAP)
    assert result.cost_eur == pytest.approx(result.grid_kwh * CHEAP)


def test_state_of_charge_chains_across_days() -> None:
    """
    Each day starts where the previous one ended — no midnight reset.

    Without chaining, energy bought in a cheap late-evening quarter
    would vanish at midnight and the answer would depend on where the
    window happened to start.
    """
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 4))
    prices = dict.fromkeys(grid, _prices(32))
    simulation = run(grid, prices, SPEC)
    assert len(simulation.days) == 4
    for previous, current in zip(simulation.days, simulation.days[1:], strict=False):
        assert current.soc_start_kwh == pytest.approx(previous.soc_end_kwh)


def test_a_pack_that_starts_full_buys_less_on_day_one() -> None:
    """
    Carried-in charge displaces buying, which is what chaining is FOR.

    On a flat repeating day the equilibrium is to end empty — the pack
    buys exactly what the day needs — so the chain shows up in what an
    opening charge saves, not in a rising state of charge.
    """
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 2))
    prices = dict.fromkeys(grid, _prices(32))
    empty = run(grid, prices, SPEC, soc_start_kwh=0.0)
    full = run(grid, prices, SPEC, soc_start_kwh=SPEC.capacity_kwh)
    assert full.days[0].soc_start_kwh == pytest.approx(SPEC.capacity_kwh)
    assert full.days[0].charged_kwh < empty.days[0].charged_kwh
    assert full.total("cost_eur") < empty.total("cost_eur")


def test_a_bigger_pack_never_costs_more() -> None:
    """Monotonicity: extra capacity cannot make the bill worse."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 3))
    prices = dict.fromkeys(grid, _prices(32))
    costs = [
        run(grid, prices, BatterySpec(capacity_kwh=cap)).total("cost_eur")
        for cap in (0.0, 5.0, 10.0, 20.0)
    ]
    assert costs == sorted(costs, reverse=True)


def test_no_battery_reproduces_the_spot_baseline() -> None:
    """A zero-capacity pack must cost exactly what buying everything costs."""
    result = simulate_day(DAY, _flat_load(24.0), _prices(32), BatterySpec(0.0))
    assert result.cost_eur == pytest.approx(result.baseline_eur)
    assert result.charged_kwh == pytest.approx(0.0)
    assert result.discharged_kwh == pytest.approx(0.0)


def test_a_day_with_no_consumption_costs_nothing() -> None:
    """An empty day must not spend or divide by zero."""
    result = simulate_day(DAY, [0.0] * QUARTERS_PER_DAY, _prices(64), SPEC)
    assert result.avg_import_price == pytest.approx(0.0)
    assert result.consumption_kwh == pytest.approx(0.0)


def test_unpriced_quarters_are_simulated_but_not_billed() -> None:
    """
    A publication gap costs nothing, mirroring the integration.

    cost.py counts energy with no known price as energy consumed but
    not as cost; the simulation follows, so a gap understates the bill
    rather than inventing a number for it.
    """
    prices: list[float | None] = [None] * QUARTERS_PER_DAY
    result = simulate_day(DAY, _flat_load(24.0), prices, SPEC)
    assert result.consumption_kwh == pytest.approx(24.0)
    assert result.cost_eur == pytest.approx(0.0)


def test_flat_consumption_spreads_the_daily_figure_over_the_window() -> None:
    """--daily-kwh fills every day in [start, end] inclusive."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 3))
    assert sorted(grid) == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    for quarters in grid.values():
        assert len(quarters) == QUARTERS_PER_DAY
        assert sum(quarters) == pytest.approx(24.0)


def test_a_single_day_window_is_one_day_not_zero() -> None:
    """A window whose start equals its end is one day, not zero."""
    grid = flat_consumption(10.0, date(2026, 9, 1), date(2026, 9, 1))
    assert list(grid) == [date(2026, 9, 1)]


def test_synthetic_load_matches_an_equivalent_meter_day() -> None:
    """The two input modes must describe the same day identically."""
    prices = _prices(cheap_quarters=32)
    synthetic = flat_consumption(24.0, DAY, DAY)[DAY]
    from_daily = simulate_day(DAY, synthetic, prices, SPEC)
    from_meter = simulate_day(DAY, _flat_load(24.0), prices, SPEC)
    assert from_daily.total_eur == pytest.approx(from_meter.total_eur)
    assert from_daily.soc_end_kwh == pytest.approx(from_meter.soc_end_kwh)


def test_months_group_in_calendar_order() -> None:
    """Days land in their own calendar month, oldest month first."""
    grid = flat_consumption(24.0, date(2026, 8, 30), date(2026, 9, 2))
    prices = dict.fromkeys(grid, _prices(32))
    months = by_month(run(grid, prices, SPEC).days)
    assert list(months) == ["2026-08", "2026-09"]
    assert len(months["2026-08"]) == 2
    assert len(months["2026-09"]) == 2


def test_monthly_totals_reconcile_with_the_window() -> None:
    """
    The rollup must account for every day exactly once.

    A summary that silently drops or double-counts a day at a month
    boundary would misstate the very comparison it exists to support.
    """
    grid = flat_consumption(24.0, date(2026, 8, 28), date(2026, 9, 5))
    prices = dict.fromkeys(grid, _prices(32))
    simulation = run(grid, prices, SPEC)
    months = by_month(simulation.days)

    assert sum(len(days) for days in months.values()) == len(simulation.days)
    for attribute in ("consumption_kwh", "grid_kwh", "cost_eur", "baseline_eur"):
        pooled = sum(
            getattr(day, attribute) for days in months.values() for day in days
        )
        assert pooled == pytest.approx(simulation.total(attribute))


def test_a_single_month_window_still_rolls_up() -> None:
    """One month is one row, not an empty table."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 3))
    prices = dict.fromkeys(grid, _prices(32))
    months = by_month(run(grid, prices, SPEC).days)
    assert list(months) == ["2026-09"]


def test_the_monthly_section_survives_hiding_the_daily_table() -> None:
    """--no-daily drops the per-day rows and keeps the rollup."""
    grid = flat_consumption(24.0, date(2026, 9, 1), date(2026, 9, 2))
    prices = dict.fromkeys(grid, _prices(32))
    simulation = run(grid, prices, SPEC)
    with_daily = report(simulation, SPEC, 0.1420, daily=True)
    without = report(simulation, SPEC, 0.1420, daily=False)
    assert "Monthly summary" in with_daily
    assert "Monthly summary" in without
    assert "Per-day simulation" in with_daily
    assert "Per-day simulation" not in without
    assert "2026-09-01" not in without  # the daily rows are gone


# --- tariff comparison -----------------------------------------------
#
# A winter week with a flat OMIE price: the TAR alone sets each
# tariff's intraday spread, so the rankings below are statements about
# the tariffs, not about market noise.

WINTER_WEEK = (date(2026, 1, 12), date(2026, 1, 18))


def _flat_omie(start: date, end: date, eur_mwh: float = 80.0) -> dict:
    """Raw OMIE per quarter, one flat price for every day in [start, end]."""
    return {
        day: [eur_mwh] * QUARTERS_PER_DAY for day in flat_consumption(1.0, start, end)
    }


INDEXADA_FIXED = 0.3462


def _all_seven(simples_fixed: float = 0.30) -> list:
    """Build the seven kinds the parameters file lists, with test values."""
    return [
        fixed("simples", flat_table(0.1420), simples_fixed, name="fixa simples"),
        fixed("bi", bi_table(0.10, 0.20), 0.30, name="fixa bi"),
        fixed("tri", tri_table(0.10, 0.18, 0.30), 0.30, name="fixa tri"),
        dynamic("tri", "semanal", INDEXADA_FIXED, name="din tri sem"),
        dynamic("tri", "diario", INDEXADA_FIXED, name="din tri dia"),
        dynamic("bi", "semanal", INDEXADA_FIXED, name="din bi sem"),
        dynamic("bi", "diario", INDEXADA_FIXED, name="din bi dia"),
    ]


def _rows(lines: list[str], runs: list) -> list[str]:
    """Tariff names in the order the comparison lists them."""
    names = [r.tariff.name for r in runs]
    return [
        line.strip().split("  ")[0]
        for line in lines
        if line.strip().split("  ")[0] in names
    ]


def test_every_tariff_is_simulated_over_the_same_days() -> None:
    """
    Including when a day has no OMIE at all.

    A tariff that skipped a day another tariff paid for would look
    cheaper by exactly that day's bill — and would be charged one fewer
    day of fixed term.
    """
    grid = flat_consumption(24.0, *WINTER_WEEK)
    omie = _flat_omie(*WINTER_WEEK)
    omie[date(2026, 1, 14)] = [None] * QUARTERS_PER_DAY  # a publication gap
    runs = compare(grid, omie, _all_seven(), SPEC)
    day_sets = {tuple(d.day for d in r.simulation.days) for r in runs}
    assert len(day_sets) == 1
    assert date(2026, 1, 14) not in next(iter(day_sets))
    assert all(r.fixed_eur == pytest.approx(r.tariff.daily_fixed_eur * 6) for r in runs)


def test_a_flat_tariff_never_cycles_the_battery() -> None:
    """
    On fixa simples the pack can only lose to round-trip losses.

    The strategy is price-driven, so this is not special-cased: the
    profitability guard simply never finds a quarter worth charging.
    """
    grid = flat_consumption(24.0, *WINTER_WEEK)
    simples = fixed("simples", flat_table(0.1420), 0.30)
    simulation = run(grid, tariff_prices(_flat_omie(*WINTER_WEEK), simples), SPEC)
    assert simulation.total("charged_kwh") == pytest.approx(0.0)
    assert simulation.total("cost_eur") == pytest.approx(
        simulation.total("baseline_eur")
    )


def test_the_battery_earns_more_on_tri_than_on_bi_horaria() -> None:
    """
    A wider spread is worth more to a pack: ponta is dearer than fora de vazio.

    Energy-only, same OMIE, same week — the ordering is the TAR tables'.
    """
    grid = flat_consumption(24.0, *WINTER_WEEK)
    runs = {
        r.tariff.name: r
        for r in compare(grid, _flat_omie(*WINTER_WEEK), _all_seven(), SPEC)
    }

    def saved(name: str) -> float:
        run_ = runs[name]
        return run_.energy_eur(battery=False) - run_.energy_eur(battery=True)

    assert saved("din tri sem") > saved("din bi sem")


def test_the_bill_is_energy_plus_the_fixed_term() -> None:
    """Total = energy with battery + daily fixed term x simulated days."""
    grid = flat_consumption(24.0, *WINTER_WEEK)
    runs = compare(grid, _flat_omie(*WINTER_WEEK), _all_seven(), SPEC)
    for tariff_run in runs:
        assert tariff_run.total_eur(battery=True) == pytest.approx(
            tariff_run.energy_eur(battery=True) + tariff_run.tariff.daily_fixed_eur * 7
        )


def test_the_comparison_ranks_by_the_whole_bill() -> None:
    """The decision view: ascending total (energy + fixed) with the battery."""
    grid = flat_consumption(24.0, *WINTER_WEEK)
    runs = compare(grid, _flat_omie(*WINTER_WEEK), _all_seven(), SPEC)
    listed = _rows(comparison_lines(runs, SPEC, "fixa simples"), runs)
    totals = {r.tariff.name: r.total_eur(battery=True) for r in runs}
    assert len(listed) == len(runs)
    assert [totals[name] for name in listed] == sorted(totals.values())


def test_a_large_fixed_term_can_outweigh_cheaper_energy() -> None:
    """
    Why the ranking is on the bill, not on energy.

    The same fixa simples offer, once with a fixed term big enough to
    lose to an indexed tariff whose ENERGY is dearer. Ranking on energy
    alone would list it first either way.
    """
    grid = flat_consumption(24.0, *WINTER_WEEK)
    omie = _flat_omie(*WINTER_WEEK)
    cheap_fixed = compare(grid, omie, _all_seven(simples_fixed=0.30), SPEC)
    dear_fixed = compare(grid, omie, _all_seven(simples_fixed=5.00), SPEC)

    def position(runs: list, name: str) -> int:
        return _rows(comparison_lines(runs, SPEC, "fixa simples"), runs).index(name)

    simples = next(r for r in dear_fixed if r.tariff.name == "fixa simples")
    indexed = next(r for r in dear_fixed if r.tariff.name == "din tri sem")
    assert simples.energy_eur(battery=True) < indexed.energy_eur(battery=True)
    assert position(dear_fixed, "fixa simples") > position(dear_fixed, "din tri sem")
    assert position(cheap_fixed, "fixa simples") < position(cheap_fixed, "din tri sem")


def test_the_reference_row_is_marked_and_zero() -> None:
    """The "vs" column is relative to the reference, so its own delta is 0."""
    grid = flat_consumption(24.0, *WINTER_WEEK)
    runs = compare(grid, _flat_omie(*WINTER_WEEK), _all_seven(), SPEC)
    row = next(
        line
        for line in comparison_lines(runs, SPEC, "fixa simples")
        if line.strip().startswith("fixa simples")
    )
    assert row.endswith("<- ref")
    assert "+0.00" in row


def test_a_flat_tariff_never_prints_a_negative_zero_saving() -> None:
    """
    Float summation noise must not read as "the battery lost money".

    `sum()` and a `+=` walk can disagree by ~1e-15 on identical costs.
    """
    grid = flat_consumption(24.0, *WINTER_WEEK)
    runs = compare(grid, _flat_omie(*WINTER_WEEK), _all_seven(), SPEC)
    lines = comparison_lines(runs, SPEC, "fixa simples")
    assert not any("-0.00" in line for line in lines)


def test_the_monthly_comparison_bills_each_month_with_its_fixed_terms() -> None:
    """
    One row per tariff, one column per month, each cell energy + fixed.

    Rows, not columns, per tariff: a parameters file can hold dozens of
    tariffs, and a column each made the table unreadable.
    """
    start, end = date(2026, 1, 30), date(2026, 2, 2)
    grid = flat_consumption(24.0, start, end)
    tariffs = _all_seven()
    runs = compare(grid, _flat_omie(start, end), tariffs, SPEC)
    lines = [line for line in monthly_comparison_lines(runs) if line.strip()]
    header, *rows = lines[1:]
    assert header.split()[-3:] == ["2026-01", "2026-02", "total"]
    assert len(rows) == len(tariffs)
    listed = [row.strip().rsplit(maxsplit=3)[0] for row in rows]
    assert sorted(listed) == sorted(t.name for t in tariffs)

    by_name = {r.tariff.name: r for r in runs}
    row = next(r for r in rows if r.strip().startswith("fixa simples"))
    january, february, total = (float(x) for x in row.split()[-3:])
    days = by_name["fixa simples"].simulation.days
    energy_jan = sum(d.cost_eur for d in days if d.day.month == 1)
    assert january == pytest.approx(energy_jan + 0.30 * 2, abs=0.005)
    assert january + february == pytest.approx(total, abs=0.011)


def test_billing_window_runs_from_day_two_to_day_two() -> None:
    """EDP's Média window: day 1 still belongs to the previous month's."""
    assert billing_window_start(date(2026, 9, 2)) == date(2026, 9, 2)
    assert billing_window_start(date(2026, 9, 30)) == date(2026, 9, 2)
    assert billing_window_start(date(2026, 10, 1)) == date(2026, 9, 2)
    assert billing_window_start(date(2026, 1, 1)) == date(2025, 12, 2)


def test_media_prices_every_quarter_at_its_windows_average() -> None:
    """Média: one price per group across the window, whatever each quarter did."""
    media = dynamic("simples", "semanal", 0.3462, pricing="indexada_media")
    sept = {
        date(2026, 9, 2): [10.0] * QUARTERS_PER_DAY,
        date(2026, 9, 3): [30.0] * QUARTERS_PER_DAY,
    }
    october = {date(2026, 10, 2): [100.0] * QUARTERS_PER_DAY}
    priced = tariff_prices({**sept, **october}, media)
    september_prices = {p for day in sept for p in priced[day]}
    assert len(september_prices) == 1  # averaged over both days: 20 EUR/MWh
    assert set(priced[date(2026, 10, 2)]) != september_prices
    horaria = dynamic("simples", "semanal", 0.3462, k1=media.k1)
    assert september_prices == {horaria.quarter_price(20.0, datetime(2026, 9, 2, 12))}
