"""
Tests for the historical OMIE ingestion (backtest/load_omie.py).

Fixtures under tests/fixtures/omie/ are real, unmodified marginalpdbc
files from omie.es. OMIE delivery days are defined in Europe/Madrid
(market time); Portugal is one hour behind, so each file's first
period lands at 23:00 of the previous day in Europe/Lisbon. Getting
this wrong shifts every price by one hour and silently corrupts every
per-period average — the granularity and timezone assertions here are
as load-bearing as the calendar tests.
"""

import itertools
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backtest.load_omie import (
    PriceRecord,
    load_series,
    monthly_period_averages,
    monthly_simples,
    parse_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "omie"
TZ_LISBON = ZoneInfo("Europe/Lisbon")


def test_parse_hourly_file() -> None:
    """Sep 2025 files are hourly: 24 records of 1.0 h each."""
    records = parse_file(FIXTURES / "marginalpdbc_20250915.1")
    assert len(records) == 24
    assert all(rec.duration_hours == 1.0 for rec in records)


def test_first_period_is_2300_lisbon_previous_day() -> None:
    """
    Period 1 = 00:00 Madrid = 23:00 Lisbon of the previous day.

    First row of the fixture reads '2025;09;15;1;97.03;97.03;'.
    """
    first = parse_file(FIXTURES / "marginalpdbc_20250915.1")[0]
    assert first.start == datetime(2025, 9, 14, 23, 0, tzinfo=TZ_LISBON)
    assert first.price_eur_mwh == 97.03


def test_parse_quarter_hourly_file() -> None:
    """From 2025-10-01 (SDAC 15-min go-live) files carry 96 periods."""
    records = parse_file(FIXTURES / "marginalpdbc_20260415.1")
    assert len(records) == 96
    assert all(rec.duration_hours == 0.25 for rec in records)
    assert records[0].start == datetime(2026, 4, 14, 23, 0, tzinfo=TZ_LISBON)
    assert records[0].price_eur_mwh == 112.45  # verbatim from the file


def test_dst_fall_back_day_has_25_hours() -> None:
    """2025-10-26 (fall back): 100 quarter-hours covering 25 hours."""
    records = parse_file(FIXTURES / "marginalpdbc_20251026.1")
    assert len(records) == 100
    assert sum(rec.duration_hours for rec in records) == 25.0
    # Continuous coverage: 23:00 Lisbon on the 25th through 23:00 on the 26th.
    assert records[0].start == datetime(2025, 10, 25, 23, 0, tzinfo=TZ_LISBON)
    last_end = records[-1].start.timestamp() + 900
    expected_end = datetime(2025, 10, 26, 23, 0, tzinfo=TZ_LISBON).timestamp()
    assert last_end == expected_end


def test_dst_spring_forward_day_has_23_hours() -> None:
    """2026-03-29 (spring forward): 92 quarter-hours covering 23 hours."""
    records = parse_file(FIXTURES / "marginalpdbc_20260329.1")
    assert len(records) == 92
    assert sum(rec.duration_hours for rec in records) == 23.0


def test_timestamps_are_strictly_increasing_in_real_time() -> None:
    """Elapsed-time ordering holds even across the DST switch."""
    for name in ("marginalpdbc_20251026.1", "marginalpdbc_20260329.1"):
        records = parse_file(FIXTURES / name)
        stamps = [rec.start.timestamp() for rec in records]
        assert stamps == sorted(stamps)
        deltas = {b - a for a, b in itertools.pairwise(stamps)}
        assert deltas == {900.0}  # every interval exactly 15 real minutes


def test_parser_rejects_prices_outside_sdac_clearing_limits(
    tmp_path: Path,
) -> None:
    """
    Values beyond ~-500..+4000 EUR/MWh are parse errors, not data.

    Per docs/findings.md "Negative OMIE prices": negative prices are
    legitimate and must flow through, but values outside the SDAC
    harmonised clearing limits can only be corruption.
    """
    for bad in ("-600", "4500"):
        target = tmp_path / "marginalpdbc_20260101.1"
        target.write_text(f"MARGINALPDBC;\n2026;01;01;1;{bad};{bad};\n*\n")
        with pytest.raises(ValueError, match="clearing limits"):
            parse_file(target)


def test_parser_accepts_negative_prices_within_limits(tmp_path: Path) -> None:
    """A legitimately negative price parses and keeps its sign."""
    target = tmp_path / "marginalpdbc_20260101.1"
    target.write_text("MARGINALPDBC;\n2026;01;01;1;-27.5;-27.5;\n*\n")
    records = parse_file(target)
    assert records[0].price_eur_mwh == -27.5


def test_load_series_reads_a_date_range() -> None:
    """load_series globs per-day files (any version suffix) in order."""
    records = load_series(FIXTURES, date(2025, 9, 15), date(2025, 9, 15))
    assert len(records) == 24
    assert records[0].start == datetime(2025, 9, 14, 23, 0, tzinfo=TZ_LISBON)


def _rec(dt: datetime, hours: float, price: float) -> PriceRecord:
    return PriceRecord(
        start=dt.replace(tzinfo=TZ_LISBON), duration_hours=hours, price_eur_mwh=price
    )


def test_monthly_period_averages_buckets_by_lisbon_month_and_period() -> None:
    """Averages bucket by (Lisbon year-month, tariff period)."""
    records = [
        _rec(datetime(2026, 7, 15, 3, 0), 1.0, 10.0),  # Wed night: vazio
        _rec(datetime(2026, 7, 15, 4, 0), 1.0, 20.0),  # Wed night: vazio
        _rec(datetime(2026, 7, 15, 10, 0), 1.0, 50.0),  # Wed morning: ponta
    ]
    averages = monthly_period_averages(records)
    assert averages[(2026, 7)]["vazio"] == pytest.approx(15.0)
    assert averages[(2026, 7)]["ponta"] == pytest.approx(50.0)


def test_monthly_period_averages_weight_by_duration() -> None:
    """An hourly record weighs four times a quarter-hourly one."""
    records = [
        _rec(datetime(2026, 7, 15, 3, 0), 1.0, 10.0),
        _rec(datetime(2026, 7, 15, 4, 0), 0.25, 20.0),
    ]
    averages = monthly_period_averages(records)
    expected = (10.0 * 1.0 + 20.0 * 0.25) / 1.25
    assert averages[(2026, 7)]["vazio"] == pytest.approx(expected)


def test_monthly_simples_is_all_hours_weighted_mean() -> None:
    """Simples ignores periods entirely: plain duration-weighted mean."""
    records = [
        _rec(datetime(2026, 7, 15, 3, 0), 1.0, 10.0),  # vazio
        _rec(datetime(2026, 7, 15, 10, 0), 1.0, 30.0),  # ponta
    ]
    assert monthly_simples(records)[(2026, 7)] == pytest.approx(20.0)
