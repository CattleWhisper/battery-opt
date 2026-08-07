"""
Tests for the tri-horária weekly-cycle period calendar.

The assertion blocks below are copied verbatim from
`docs/tariff-reference.md` §4 — they are the specification, not examples.
The calendar is the highest-risk module in the system: a wrong boundary
discharges into cheias for six months without raising any error.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.battery_opt.core.calendar import (
    CALENDARS,
    Calendars,
    period,
    season,
    season_switch,
    weekly_hours,
)


def _iterated_week_hours(monday: date) -> dict[str, float]:
    """
    Sum hours per period over a full week by minute-wise iteration.

    Independent of the span-table arithmetic in `weekly_hours` — this
    walks every minute of a real week through `period()`.
    """
    assert monday.weekday() == 0
    totals = {"ponta": 0.0, "cheias": 0.0, "vazio": 0.0}
    dt = datetime(monday.year, monday.month, monday.day)
    for minute in range(7 * 24 * 60):
        totals[period(dt + timedelta(minutes=minute))] += 1 / 60
    return {name: round(hours, 6) for name, hours in totals.items()}


def test_summer_weekday_boundaries() -> None:
    """Summer Mon-Fri: single morning ponta window, 09:15-12:15."""
    assert period(datetime(2026, 7, 15, 9, 14)) == "cheias"  # 1 min before ponta
    assert period(datetime(2026, 7, 15, 9, 15)) == "ponta"  # exact start
    assert period(datetime(2026, 7, 15, 12, 14)) == "ponta"  # 1 min before end
    assert period(datetime(2026, 7, 15, 12, 15)) == "cheias"  # exact end
    assert period(datetime(2026, 7, 15, 19, 0)) == "cheias"  # no evening ponta


def test_summer_weekend() -> None:
    """Summer Saturday has no ponta; Sunday is entirely vazio."""
    assert period(datetime(2026, 7, 18, 10, 0)) == "cheias"  # Saturday: no ponta
    assert period(datetime(2026, 7, 18, 15, 0)) == "vazio"  # Saturday afternoon
    assert period(datetime(2026, 7, 19, 10, 0)) == "vazio"  # Sunday: all vazio


def test_winter_weekday_boundaries() -> None:
    """Winter Mon-Fri: two ponta windows, 09:30-12:00 and 18:30-21:00."""
    assert period(datetime(2026, 1, 15, 9, 29)) == "cheias"
    assert period(datetime(2026, 1, 15, 9, 30)) == "ponta"
    assert period(datetime(2026, 1, 15, 12, 0)) == "cheias"
    assert period(datetime(2026, 1, 15, 19, 0)) == "ponta"  # evening ponta
    assert period(datetime(2026, 1, 15, 21, 0)) == "cheias"


def test_winter_evening_ponta_start_boundary() -> None:
    """The 18:30 boundary, minute-exact (Checkpoint A criterion)."""
    assert period(datetime(2026, 1, 15, 18, 29)) == "cheias"  # 1 min before
    assert period(datetime(2026, 1, 15, 18, 30)) == "ponta"  # exact start


def test_winter_weekend() -> None:
    """Winter Saturday has no ponta; Sunday is entirely vazio."""
    assert period(datetime(2026, 1, 17, 19, 0)) == "cheias"  # Saturday
    assert period(datetime(2026, 1, 18, 19, 0)) == "vazio"  # Sunday


def test_season_switches() -> None:
    """Season flips on the last Sunday of March and of October."""
    assert season(date(2026, 3, 28)) == "winter"  # day before
    assert season(date(2026, 3, 29)) == "summer"  # last Sunday of March
    assert season(date(2026, 10, 24)) == "summer"
    assert season(date(2026, 10, 25)) == "winter"  # last Sunday of October


def test_season_switch_detects_only_the_two_switch_days() -> None:
    """season_switch names the season that BEGINS on a switch day."""
    assert season_switch(date(2026, 3, 29)) == "summer"
    assert season_switch(date(2026, 10, 25)) == "winter"
    assert season_switch(date(2026, 3, 28)) is None  # day before
    assert season_switch(date(2026, 3, 30)) is None  # day after
    assert season_switch(date(2026, 7, 15)) is None  # mid-season


def test_weekly_totals() -> None:
    """Weekly totals — the test that catches structural errors."""
    assert weekly_hours("summer")["ponta"] == 15
    assert weekly_hours("winter")["ponta"] == 25
    assert weekly_hours("summer")["vazio"] == 76
    assert weekly_hours("winter")["vazio"] == 76


def test_weekly_totals_by_iteration_summer() -> None:
    """Iterating every minute of a real summer week matches 76/15/77."""
    totals = _iterated_week_hours(date(2026, 7, 13))  # Mon 13 Jul 2026
    assert totals == {"vazio": 76.0, "ponta": 15.0, "cheias": 77.0}


def test_weekly_totals_by_iteration_winter() -> None:
    """Iterating every minute of a real winter week matches 76/25/67."""
    totals = _iterated_week_hours(date(2026, 1, 12))  # Mon 12 Jan 2026
    assert totals == {"vazio": 76.0, "ponta": 25.0, "cheias": 67.0}


def test_flat_load_ponta_share_matches_invoice() -> None:
    """
    Real-data validation: flat load in summer puts ~8.93% in ponta.

    The EDP invoice for 2 Jun - 1 Jul 2026 measured 56 of 644 kWh in
    ponta (8.7%). The model figure for a flat load is the whole-week
    share, 15/168 = 8.93% — computed here by iteration, not hardcoded.
    Docs (CONTEXT.md) call this 2.6% deviation; tolerance is 3%.

    Note: iterating the literal 30-day invoice window (Tue 2 Jun - Wed
    1 Jul, i.e. 4 whole weeks plus a Tue and a Wed) gives 66/720 h =
    9.17%, because the extra days both carry ponta. The documented
    validation is against the whole-week figure, which is also what a
    30-day billing average approximates over a mix of start weekdays.
    """
    totals = _iterated_week_hours(date(2026, 6, 15))  # Mon 15 Jun 2026
    model_share = totals["ponta"] / sum(totals.values())
    assert model_share == pytest.approx(15 / 168)
    measured_share = 56 / 644  # 8.7%, from the invoice
    assert model_share == pytest.approx(measured_share, rel=0.03)


def test_calendar_versioned_by_effective_date() -> None:
    """A 2027 table is a data addition, not a code change (ADR-0005)."""
    all_vazio = dict.fromkeys(("weekday", "saturday", "sunday"), ((0, 1440, "vazio"),))
    reform_table = {"summer": all_vazio, "winter": all_vazio}
    calendars: Calendars = (*CALENDARS, (date(2027, 1, 1), reform_table))
    # 2026 instants still use the 2025/26 table...
    assert period(datetime(2026, 7, 15, 10, 0), calendars) == "ponta"
    # ...and 2027 instants pick up the new one.
    assert period(datetime(2027, 7, 15, 10, 0), calendars) == "vazio"


def test_dates_before_first_calendar_raise() -> None:
    """No silent extrapolation before the earliest effective date."""
    with pytest.raises(ValueError, match="No tariff calendar in force"):
        period(datetime(2024, 12, 31, 12, 0))


def test_aware_datetimes_convert_to_portugal_time() -> None:
    """08:20 UTC in July is 09:20 in Lisbon (WEST) — inside ponta."""
    assert period(datetime(2026, 7, 15, 8, 20, tzinfo=UTC)) == "ponta"
    assert period(datetime(2026, 7, 15, 8, 10, tzinfo=UTC)) == "cheias"


def test_dst_switch_days_are_sundays_all_vazio() -> None:
    """Both DST switch days are Sundays, hence entirely vazio."""
    assert period(datetime(2026, 3, 29, 2, 30)) == "vazio"  # spring forward
    assert period(datetime(2026, 10, 25, 2, 30)) == "vazio"  # fall back
