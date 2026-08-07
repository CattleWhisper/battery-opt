"""
Tests for the OMIE price-source parser (prices_source.py).

HA core's `omie` integration returns the day-ahead series from its
`get_prices_for_date` service as [{"start": CET ISO datetime, "end",
"price": EUR/kWh}, ...] per market date (verified from
home-assistant/core sources). The parser assembles one Lisbon-local
day of delivered EUR/kWh prices from those entries.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_opt.core.prices import price
from custom_components.battery_opt.prices_source import day_price_vector_from_service

TZ = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 7, 15)  # summer Wednesday


def _service_entries(market_date: date, price_kwh: float = 0.06) -> list[dict]:
    """Core-OMIE-shaped service entries: CET starts, EUR/kWh prices."""
    cet = ZoneInfo("Europe/Madrid")
    midnight = datetime(
        market_date.year, market_date.month, market_date.day, tzinfo=cet
    )
    return [
        {
            "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
            "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
            "price": price_kwh,
        }
        for i in range(96)
    ]


def test_service_response_builds_a_full_lisbon_day() -> None:
    """
    Market dates D and D+1 assemble the Lisbon-local day D.

    Core OMIE keys by CET: market day D covers 23:00 D-1 to 23:00 D
    in Lisbon, so the local day's last hour comes from market D+1.
    """
    vector, padded = day_price_vector_from_service(
        DAY,
        _service_entries(DAY) + _service_entries(DAY + timedelta(days=1)),
    )
    assert vector is not None
    assert padded is False
    assert len(vector) == 96
    # 0.06 EUR/kWh = 60 EUR/MWh through the same formula as everywhere:
    # 03:00 is vazio, 10:00 is summer ponta.
    assert vector[12] == pytest.approx(
        price(60.0, datetime(2026, 7, 15, 3, 0, tzinfo=TZ))
    )
    assert vector[40] == pytest.approx(
        price(60.0, datetime(2026, 7, 15, 10, 0, tzinfo=TZ))
    )


def test_service_response_pads_the_tail_before_dplus1_publishes() -> None:
    """
    Pad the tail before market D+1 publishes (~13:30 CET).

    The last Lisbon hour repeats the final known price and the
    result is flagged as padded.
    """
    vector, padded = day_price_vector_from_service(DAY, _service_entries(DAY))
    assert vector is not None
    assert padded is True
    assert len(vector) == 96
    assert vector[95] == vector[91]  # tail repeats the last known price


def test_service_response_missing_day_returns_none() -> None:
    """Entries for another day only: not plannable."""
    vector, padded = day_price_vector_from_service(
        DAY, _service_entries(DAY - timedelta(days=5))
    )
    assert vector is None
    assert padded is False


def test_mid_day_gap_is_an_error_not_a_shifted_vector() -> None:
    """
    A non-tail gap must refuse to build the day (no silent shift).

    Consumers index the vector positionally by quarter-hour; padding a
    mid-day hole at the tail would move every later price one slot
    early. The parser errors out instead.
    """
    entries = _service_entries(DAY) + _service_entries(DAY + timedelta(days=1))
    with_gap = [e for i, e in enumerate(entries) if i != 40]  # drop one quarter
    vector, padded = day_price_vector_from_service(DAY, with_gap)
    assert vector is None
    assert padded is False


def test_head_gap_is_an_error() -> None:
    """A day whose first quarters are missing is refused, not padded."""
    entries = _service_entries(DAY) + _service_entries(DAY + timedelta(days=1))
    headless = entries[8:]  # first two Lisbon hours missing
    vector, padded = day_price_vector_from_service(DAY, headless)
    assert vector is None
    assert padded is False


def test_negative_prices_flow_through() -> None:
    """OMIE below zero stays below zero in the market term (no clamp)."""
    vector, _ = day_price_vector_from_service(
        DAY,
        _service_entries(DAY, price_kwh=-0.045)
        + _service_entries(DAY + timedelta(days=1), price_kwh=-0.045),
    )
    assert vector is not None
    expected = price(-45.0, datetime(2026, 7, 15, 3, 0, tzinfo=TZ))
    assert vector[12] == pytest.approx(expected)
    assert expected < 0.02  # sanity: genuinely reflects the negative spot
