"""
Tests for the OMIE price-source parser (prices_source.py).

The hass_omie integration (verified from source, luuuis/hass_omie
sensor.py) exposes `today_hours` / `tomorrow_hours` attributes as
dicts of local quarter-hour-start datetimes -> EUR/MWh spot prices,
plus `*_provisional` flags; values may be None while provisional.
The parser turns one local day of that into the delivered EUR/kWh
vector the optimiser consumes — or None when the day is incomplete.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_opt.core.prices import price
from custom_components.battery_opt.prices_source import day_price_vector

TZ = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 7, 15)  # summer Wednesday


def _quarter_hours(day: date, omie_eur_mwh: float = 60.0) -> dict:
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return {midnight + timedelta(minutes=15 * i): omie_eur_mwh for i in range(96)}


def test_full_day_becomes_96_delivered_prices() -> None:
    """96 quarter-hours of OMIE 60 through the EDP formula."""
    attributes = {"today_hours": _quarter_hours(DAY)}
    vector = day_price_vector(attributes, DAY)
    assert vector is not None
    assert len(vector) == 96
    # 03:00 is vazio; 10:00 (index 40) is summer ponta.
    assert vector[12] == pytest.approx(
        price(60.0, datetime(2026, 7, 15, 3, 0, tzinfo=TZ))
    )
    assert vector[40] == pytest.approx(
        price(60.0, datetime(2026, 7, 15, 10, 0, tzinfo=TZ))
    )


def test_day_is_found_in_tomorrow_hours_too() -> None:
    """The parser searches both attribute dicts for the wanted day."""
    attributes = {
        "today_hours": _quarter_hours(DAY - timedelta(days=1)),
        "tomorrow_hours": _quarter_hours(DAY),
    }
    assert day_price_vector(attributes, DAY) is not None


def test_incomplete_day_returns_none() -> None:
    """Provisional data (None values) is not a plannable day."""
    hours = _quarter_hours(DAY)
    hours[datetime(2026, 7, 15, 22, 0, tzinfo=TZ)] = None
    assert day_price_vector({"today_hours": hours}, DAY) is None


def test_missing_day_returns_none() -> None:
    """No attributes, or the wrong day: no vector."""
    assert day_price_vector({}, DAY) is None
    assert day_price_vector({"today_hours": None}, DAY) is None
    attributes = {"today_hours": _quarter_hours(DAY - timedelta(days=1))}
    assert day_price_vector(attributes, DAY) is None


def test_negative_prices_flow_through() -> None:
    """OMIE below zero stays below zero in the market term (no clamp)."""
    attributes = {"today_hours": _quarter_hours(DAY, omie_eur_mwh=-45.0)}
    vector = day_price_vector(attributes, DAY)
    assert vector is not None
    expected = price(-45.0, datetime(2026, 7, 15, 3, 0, tzinfo=TZ))
    assert vector[12] == pytest.approx(expected)
    assert expected < 0.02  # sanity: genuinely reflects the negative spot


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
    from custom_components.battery_opt.prices_source import (  # noqa: PLC0415
        day_price_vector_from_service,
    )

    vector, padded = day_price_vector_from_service(
        DAY,
        _service_entries(DAY) + _service_entries(DAY + timedelta(days=1)),
    )
    assert vector is not None
    assert padded is False
    assert len(vector) == 96
    # 0.06 EUR/kWh = 60 EUR/MWh through the same formula as everywhere.
    assert vector[12] == pytest.approx(
        price(60.0, datetime(2026, 7, 15, 3, 0, tzinfo=TZ))
    )


def test_service_response_pads_the_tail_before_dplus1_publishes() -> None:
    """
    Pad the tail before market D+1 publishes (~13:30 CET).

    The last Lisbon hour repeats the final known price and the
    result is flagged as padded.
    """
    from custom_components.battery_opt.prices_source import (  # noqa: PLC0415
        day_price_vector_from_service,
    )

    vector, padded = day_price_vector_from_service(DAY, _service_entries(DAY))
    assert vector is not None
    assert padded is True
    assert len(vector) == 96
    assert vector[95] == vector[91]  # tail repeats the last known price


def test_service_response_missing_day_returns_none() -> None:
    """Entries for another day only: not plannable."""
    from custom_components.battery_opt.prices_source import (  # noqa: PLC0415
        day_price_vector_from_service,
    )

    vector, padded = day_price_vector_from_service(
        DAY, _service_entries(DAY - timedelta(days=5))
    )
    assert vector is None
    assert padded is False


def test_hourly_fallback_expands_to_quarter_hours() -> None:
    """24 hourly entries expand x4 (worth ~2% per the Task 6 finding)."""
    midnight = datetime(DAY.year, DAY.month, DAY.day, tzinfo=TZ)
    hourly = {midnight + timedelta(hours=h): 60.0 for h in range(24)}
    vector = day_price_vector({"today_hours": hourly}, DAY)
    assert vector is not None
    assert len(vector) == 96
    assert vector[0] == vector[1] == vector[2] == vector[3]
