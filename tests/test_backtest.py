"""
Tests for the backtest harness (backtest/run.py, backtest/report.py).

The harness's own logic — day-grouping, SoC day-chaining, annualization,
cycle counting, CSV — is tested on synthetic price records; cost
accounting itself is the Task 5 evaluator, deliberately not
reimplemented here (two cost models drifting apart is the classic
backtest bug). Full-data runs are exercised by the Checkpoint B smoke
test, gated on the local OMIE download.
"""

import csv
import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backtest.load_omie import PriceRecord
from backtest.report import DayResult, annualize, season_weights, write_csv
from backtest.run import (
    group_by_local_day,
    media_price_model,
    simulate,
    to_hourly,
)
from custom_components.battery_opt.core.plan import BatteryParams

TZ_LISBON = ZoneInfo("Europe/Lisbon")
PARAMS = BatteryParams()


def _day_records(day: date, omie_eur_mwh: float = 60.0) -> list[PriceRecord]:
    """Synthetic quarter-hourly records covering one local day."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ_LISBON)
    return [
        PriceRecord(
            start=midnight + timedelta(minutes=15 * i),
            duration_hours=0.25,
            price_eur_mwh=omie_eur_mwh,
        )
        for i in range(96)
    ]


def test_group_by_local_day_splits_on_lisbon_midnight() -> None:
    """Records bucket by their Lisbon-local date, sorted within the day."""
    two_days = _day_records(date(2026, 7, 15)) + _day_records(date(2026, 7, 16))
    groups = group_by_local_day(two_days)
    assert sorted(groups) == [date(2026, 7, 15), date(2026, 7, 16)]
    assert len(groups[date(2026, 7, 15)]) == 96
    starts = [rec.start for rec in groups[date(2026, 7, 15)]]
    assert starts == sorted(starts)


def test_simulate_chains_soc_across_days() -> None:
    """
    Static summer carryover: midday charge crosses midnight.

    Day 1 starts at the floor, misses its morning ponta, charges at
    midday; day 2 starts full and serves its morning ponta.
    """
    days = [
        (date(2026, 7, 15), _day_records(date(2026, 7, 15))),  # Wednesday
        (date(2026, 7, 16), _day_records(date(2026, 7, 16))),  # Thursday
    ]
    results = simulate(days, "static", PARAMS)
    assert results[0].discharge_kwh == 0  # starts at the floor: nothing to give
    assert results[0].end_soc_kwh > 4.9  # midday charge filled it
    assert results[1].discharge_kwh > 3.0  # morning ponta served from carryover


def test_group_by_local_day_with_shifted_boundary() -> None:
    """
    boundary_hour=13 splits planning days at 13:00 local.

    The 12:45 record belongs to the previous planning day; the 13:00
    record opens a new one. Used to quantify open question #4 (the
    midnight boundary blocks midday-charge -> next-morning-ponta
    pairing for the per-day greedy).
    """
    records = _day_records(date(2026, 7, 15)) + _day_records(date(2026, 7, 16))
    groups = group_by_local_day(records, boundary_hour=13)
    # Three planning days: [.., 15th 13:00), [15th 13:00, 16th 13:00), [16th 13:00, ..)
    assert sorted(groups) == [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]
    middle = groups[date(2026, 7, 15)]
    assert len(middle) == 96
    assert middle[0].start.hour == 13
    assert middle[-1].start.hour == 12


def test_simulate_handles_negative_prices() -> None:
    """A day with negative OMIE flows through with no special-casing."""
    day = date(2026, 4, 15)  # Wednesday
    records = _day_records(day, omie_eur_mwh=-45.0)
    results = simulate([(day, records)], "greedy", PARAMS)
    assert results[0].consumption_kwh == pytest.approx(24.96)
    # Delivered vazio price at OMIE -45 is negative; base cost may be
    # negative in parts — the harness must not clamp it.
    assert results[0].base_cost_eur < 5.0


def test_season_weights_sum_to_a_year() -> None:
    """154 winter + 211 summer days in the Sep 2025 - Aug 2026 year."""
    weights = season_weights()
    assert weights == {"winter": 154, "summer": 211}


def _result(day: date, season: str, saving: float) -> DayResult:
    return DayResult(
        day=day,
        season=season,
        intervals=96,
        interval_hours=0.25,
        consumption_kwh=24.96,
        base_cost_eur=3.0,
        cost_eur=3.0 - saving,
        saving_eur=saving,
        charge_kwh=4.0,
        discharge_kwh=3.6,
        discharge_battery_kwh=3.6 / (0.9**0.5),
        end_soc_kwh=1.35,
    )


def test_annualize_weights_by_season() -> None:
    """
    One winter day at 1.00, one summer day at 0.50 EUR of saving.

    Annual = 154 x 1.00 + 211 x 0.50 = 259.0 (excl VAT).
    """
    results = [
        _result(date(2026, 1, 15), "winter", 1.00),
        _result(date(2026, 7, 15), "summer", 0.50),
    ]
    annual = annualize(results, PARAMS)
    assert annual["annual_saving_eur"] == pytest.approx(154 + 211 * 0.5)
    assert annual["annual_saving_eur_incl_vat"] == pytest.approx(259.5 * 1.23)


def test_annualize_counts_cycles_from_battery_side_throughput() -> None:
    """Cycles = annual SoC-side discharge / usable capacity."""
    results = [_result(date(2026, 1, 15), "winter", 1.0)]
    annual = annualize(results, PARAMS)
    expected_battery_kwh = 3.6 / (0.9**0.5) * 154  # winter days only
    assert annual["annual_discharge_battery_kwh"] == pytest.approx(expected_battery_kwh)
    assert annual["cycles_per_year"] == pytest.approx(expected_battery_kwh / 5.0)


def test_write_csv_round_trips(tmp_path: Path) -> None:
    """Per-day rows land in the CSV with all fields."""
    results = [_result(date(2026, 1, 15), "winter", 1.0)]
    target = tmp_path / "out.csv"
    write_csv(results, target)
    with target.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-01-15"
    assert float(rows[0]["saving_eur"]) == pytest.approx(1.0)


def test_to_hourly_averages_quarters() -> None:
    """Four quarters collapse to one hour at their mean price."""
    day = date(2026, 4, 15)
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ_LISBON)
    quarters = [
        PriceRecord(
            start=midnight + timedelta(minutes=15 * i),
            duration_hours=0.25,
            price_eur_mwh=float(price),
        )
        for i, price in enumerate([10.0, 20.0, 30.0, 40.0])
    ]
    hourly = to_hourly(quarters)
    assert len(hourly) == 1
    assert hourly[0].duration_hours == 1.0
    assert hourly[0].price_eur_mwh == pytest.approx(25.0)
    assert hourly[0].start == midnight


def test_media_model_prices_at_window_period_average() -> None:
    """
    Média: every vazio quarter in the window gets the same price.

    Two days with different OMIE levels: under Média both days' vazio
    intervals price at the window's vazio average, not their own OMIE.
    """
    day_a, day_b = date(2026, 7, 15), date(2026, 7, 16)
    records = _day_records(day_a, 40.0) + _day_records(day_b, 80.0)
    model = media_price_model(records, k1=1.10)
    vazio_a = model(records[12])  # 03:00 on day A
    vazio_b = model(records[96 + 12])  # 03:00 on day B
    assert vazio_a == pytest.approx(vazio_b)
    # And the level reflects the average OMIE (60), not either day's own.
    expected = 60.0 / 1000 * 1.164 * 1.10 + 0.0185 + 0.0158
    assert vazio_a == pytest.approx(expected)


def test_plan_wear_prunes_cycles_but_savings_use_true_wear() -> None:
    """
    A planning wear above the true wear trades saving for cycles.

    The optimiser plans with the inflated wear (fewer, more selective
    pairs); the evaluator always books the true 0.020. This is the
    cheias-cycling cap mechanism from the Checkpoint B decisions.
    """
    day = date(2026, 1, 15)  # winter Thursday
    # Alternating cheap/dear quarters give many marginal pairs.
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ_LISBON)
    records = [
        PriceRecord(
            start=midnight + timedelta(minutes=15 * i),
            duration_hours=0.25,
            price_eur_mwh=30.0 if i % 8 < 4 else 90.0,
        )
        for i in range(96)
    ]
    unlimited = simulate([(day, records)], "greedy", PARAMS)
    pruned = simulate([(day, records)], "greedy", PARAMS, plan_wear=0.10)
    assert pruned[0].discharge_kwh < unlimited[0].discharge_kwh
    # Both evaluated at the true wear: the pruned run keeps most value.
    assert 0 < pruned[0].saving_eur <= unlimited[0].saving_eur


def test_greedy_chain_carries_no_stale_soc_between_runs() -> None:
    """Two simulate() calls are independent: no hidden state."""
    day = date(2026, 7, 15)
    days = [(day, _day_records(day))]
    first = simulate(days, "greedy", PARAMS)
    second = simulate(days, "greedy", PARAMS)
    assert first == second


_HAVE_DATA = (Path(__file__).parent.parent / "backtest" / "data" / "omie").exists()


@pytest.mark.skipif(not _HAVE_DATA, reason="run backtest/download_omie.py first")
def test_full_window_runs_under_ten_seconds() -> None:
    """
    Task 6 acceptance: the full window replays in <10 s, plans valid.

    simulate() raises on any C-1..C-7 violation, so completing IS the
    validity assertion.
    """
    import time  # noqa: PLC0415

    from backtest.load_omie import DATA_DIR, load_series  # noqa: PLC0415
    from backtest.run import DEFAULT_FIRST_DAY, DEFAULT_LAST_DAY  # noqa: PLC0415

    started = time.monotonic()
    records = load_series(DATA_DIR, DEFAULT_FIRST_DAY, DEFAULT_LAST_DAY)
    groups = group_by_local_day(records)
    days = [(day, groups[day]) for day in sorted(groups)]
    greedy = simulate(days, "greedy", PARAMS)
    static = simulate(days, "static", PARAMS)
    assert time.monotonic() - started < 10.0
    assert sum(r.saving_eur for r in greedy) >= sum(r.saving_eur for r in static)


def test_simulate_respects_capacity_parameter() -> None:
    """cap_usable flows through: a 10 kWh run can store more."""
    day_1, day_2 = date(2026, 1, 15), date(2026, 1, 16)
    days = [
        (day_1, _day_records(day_1, 20.0)),
        (day_2, _day_records(day_2, 20.0)),
    ]
    big = dataclasses.replace(PARAMS, cap_usable_kwh=10.0)
    small_results = simulate(days, "static", PARAMS)
    big_results = simulate(days, "static", big)
    assert sum(r.charge_kwh for r in big_results) > sum(
        r.charge_kwh for r in small_results
    )
