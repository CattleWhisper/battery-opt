"""
Tests for core/reporting.py (plan Task 13, pure layer).

RealisedDay integrates measured battery power (charge-loop sign
convention: positive W = charging) into one day's realised saving;
MonthLedger folds days into the monthly forecast-vs-realised
comparison and renders the report the notification carries.
"""

import pytest

from custom_components.battery_opt.core.reporting import (
    DEVIATION_ALERT_PCT,
    MonthLedger,
    RealisedDay,
)

WEAR = 0.020


def _day(day: str = "2026-08-08") -> RealisedDay:
    return RealisedDay(day=day, wear_cost_eur_kwh=WEAR)


def test_discharge_books_value_minus_wear() -> None:
    """1 kWh discharged at 0.30: value 0.30, wear 0.02 -> 0.28 net."""
    day = _day()
    day.add_interval(-1000.0, 1.0, 0.30)
    assert day.discharged_kwh == pytest.approx(1.0)
    assert day.charged_kwh == 0.0
    assert day.realised_eur == pytest.approx(0.30 - WEAR)


def test_charge_books_cost() -> None:
    """2 kWh charged at 0.10 costs 0.20; no wear on charge."""
    day = _day()
    day.add_interval(2000.0, 1.0, 0.10)
    assert day.charged_kwh == pytest.approx(2.0)
    assert day.realised_eur == pytest.approx(-0.20)


def test_full_cycle_hand_computed() -> None:
    """Charge 0.5 kWh at 0.10, discharge 0.25 kWh at 0.30 (cf. plan.py)."""
    day = _day()
    day.add_interval(2000.0, 0.25, 0.10)  # 0.5 kWh in
    day.add_interval(-1000.0, 0.25, 0.30)  # 0.25 kWh out
    assert day.realised_eur == pytest.approx(0.30 * 0.25 - 0.10 * 0.5 - WEAR * 0.25)


def test_unpriced_energy_counts_as_flow_not_cash() -> None:
    """Price None (post-midnight window): kWh counted, EUR untouched."""
    day = _day()
    day.add_interval(-1000.0, 1.0, None)
    assert day.discharged_kwh == pytest.approx(1.0)
    # Wear still applies: the cycle happened even if unpriced.
    assert day.realised_eur == pytest.approx(-WEAR)


def test_zero_power_and_zero_dt_are_no_ops() -> None:
    """Idle samples and non-positive intervals contribute nothing."""
    day = _day()
    day.add_interval(0.0, 1.0, 0.30)
    day.add_interval(-1000.0, 0.0, 0.30)
    day.add_interval(-1000.0, -1.0, 0.30)
    assert day.realised_eur == 0.0


def test_realised_day_store_round_trip() -> None:
    """as_dict/from_dict is lossless."""
    day = _day()
    day.add_interval(-1500.0, 0.5, 0.25)
    rebuilt = RealisedDay.from_dict(day.as_dict())
    assert rebuilt == day


def test_ledger_folds_only_its_own_month() -> None:
    """A stray other-month day (restart edge) never pollutes the sums."""
    ledger = MonthLedger(month="2026-08")
    august = _day("2026-08-08")
    august.add_interval(-1000.0, 1.0, 0.30)
    july = _day("2026-07-31")
    july.add_interval(-1000.0, 1.0, 0.30)
    ledger.fold_day(august)
    ledger.fold_day(july)
    assert list(ledger.realised_daily) == ["2026-08-08"]
    ledger.record_forecast("2026-07-31", 1.0)
    assert ledger.forecast_daily == {}


def test_ledger_forecast_refresh_overwrites_same_day() -> None:
    """Each coordinator refresh updates the day's estimate in place."""
    ledger = MonthLedger(month="2026-08")
    ledger.record_forecast("2026-08-08", 0.50)
    ledger.record_forecast("2026-08-08", 0.85)
    assert ledger.forecast_eur == pytest.approx(0.85)


def test_deviation_none_without_forecast() -> None:
    """No forecast recorded yet: deviation undefined, report says n/a."""
    ledger = MonthLedger(month="2026-08")
    assert ledger.deviation_pct() is None
    assert "n/a" in ledger.report()


def test_deviation_above_threshold_is_flagged_in_the_report() -> None:
    """Spec Task 13: >10% deviation raises the loud flag."""
    ledger = MonthLedger(month="2026-08")
    ledger.record_forecast("2026-08-08", 1.00)
    day = _day("2026-08-08")
    day.add_interval(-5000.0, 1.0, 0.30)  # realised 1.40 -> +40%
    ledger.fold_day(day)
    deviation = ledger.deviation_pct()
    assert deviation is not None
    assert deviation > DEVIATION_ALERT_PCT
    assert "ABOVE the 10%" in ledger.report()


def test_deviation_within_threshold_is_not_flagged() -> None:
    """A small deviation reports quietly."""
    ledger = MonthLedger(month="2026-08")
    ledger.record_forecast("2026-08-08", 1.00)
    day = _day("2026-08-08")
    day.discharge_value_eur = 1.05  # +5% hand-set
    ledger.fold_day(day)
    assert "ABOVE" not in ledger.report()
    assert "+5.0%" in ledger.report()


def test_ledger_store_round_trip() -> None:
    """as_dict/from_dict is lossless."""
    ledger = MonthLedger(month="2026-08")
    ledger.record_forecast("2026-08-08", 0.5)
    day = _day("2026-08-08")
    day.add_interval(-1000.0, 1.0, 0.30)
    ledger.fold_day(day)
    rebuilt = MonthLedger.from_dict(ledger.as_dict())
    assert rebuilt == ledger
