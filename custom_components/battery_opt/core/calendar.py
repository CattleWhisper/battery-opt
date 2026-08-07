"""
Tri-horária weekly-cycle tariff period calendar (ERSE, BTN, Portugal).

Pure functions: no I/O, no clock reads, no mutable globals. Times are
Portugal continental legal time (Europe/Lisbon). Naive datetimes are
interpreted as local legal time; aware datetimes are converted.

The calendar is a data structure indexed by effective date (ADR-0005),
never hardcoded to a year. Period names (ponta, cheias, vazio) are ERSE
regulatory terms and stay in Portuguese. Source tables:
`docs/tariff-reference.md` §3.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Period = Literal["ponta", "cheias", "vazio"]
Season = Literal["summer", "winter"]
DayClass = Literal["weekday", "saturday", "sunday"]

# Spans are (start_minute, end_minute, period), half-open [start, end),
# minutes from local midnight; 1440 = 24:00. Each day class must cover
# the full day — validated by weekly-total tests iterating real weeks.
DaySpans = tuple[tuple[int, int, Period], ...]
CalendarTable = dict[Season, dict[DayClass, DaySpans]]

TZ_PORTUGAL = ZoneInfo("Europe/Lisbon")

_SATURDAY = 5
_SUNDAY = 6
_MINUTES_PER_DAY = 1440

# Tri-horária, ciclo semanal — docs/tariff-reference.md §3.
# Hour structure valid for 2025 and 2026: the project's MA30 reference
# series (Sep 2025 - Aug 2026) applies exactly this mapping. The ERSE
# reform expected ~Jan 2027 gets a new entry in CALENDARS, not an edit.
_WEEKLY_2025: CalendarTable = {
    "summer": {
        "weekday": (
            (0, 7 * 60, "vazio"),  # 00:00-07:00
            (7 * 60, 9 * 60 + 15, "cheias"),  # 07:00-09:15
            (9 * 60 + 15, 12 * 60 + 15, "ponta"),  # 09:15-12:15
            (12 * 60 + 15, 1440, "cheias"),  # 12:15-24:00
        ),
        "saturday": (
            (0, 9 * 60, "vazio"),  # 00:00-09:00
            (9 * 60, 14 * 60, "cheias"),  # 09:00-14:00
            (14 * 60, 20 * 60, "vazio"),  # 14:00-20:00
            (20 * 60, 22 * 60, "cheias"),  # 20:00-22:00
            (22 * 60, 1440, "vazio"),  # 22:00-24:00
        ),
        "sunday": ((0, 1440, "vazio"),),
    },
    "winter": {
        "weekday": (
            (0, 7 * 60, "vazio"),  # 00:00-07:00
            (7 * 60, 9 * 60 + 30, "cheias"),  # 07:00-09:30
            (9 * 60 + 30, 12 * 60, "ponta"),  # 09:30-12:00
            (12 * 60, 18 * 60 + 30, "cheias"),  # 12:00-18:30
            (18 * 60 + 30, 21 * 60, "ponta"),  # 18:30-21:00
            (21 * 60, 1440, "cheias"),  # 21:00-24:00
        ),
        "saturday": (
            (0, 9 * 60 + 30, "vazio"),  # 00:00-09:30
            (9 * 60 + 30, 13 * 60, "cheias"),  # 09:30-13:00
            (13 * 60, 18 * 60 + 30, "vazio"),  # 13:00-18:30
            (18 * 60 + 30, 22 * 60, "cheias"),  # 18:30-22:00
            (22 * 60, 1440, "vazio"),  # 22:00-24:00
        ),
        "sunday": ((0, 1440, "vazio"),),
    },
}

Calendars = tuple[tuple[date, CalendarTable], ...]

CALENDARS: Calendars = ((date(2025, 1, 1), _WEEKLY_2025),)


def last_sunday(year: int, month: int) -> date:
    """Return the last Sunday of the given month."""
    december = 12
    nxt = date(year + 1, 1, 1) if month == december else date(year, month + 1, 1)
    last_day = nxt - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - _SUNDAY) % 7)


def season(d: date) -> Season:
    """
    Return the tariff season for a date.

    Summer runs from the last Sunday of March (inclusive) to the last
    Sunday of October (exclusive) — aligned with the DST switches, and
    both switch days are Sundays, hence entirely vazio.
    """
    if last_sunday(d.year, 3) <= d < last_sunday(d.year, 10):
        return "summer"
    return "winter"


def season_switch(d: date) -> Season | None:
    """
    Return the season that BEGINS on `d` if it is a switch day, else None.

    Feeds the spec §9 seasonal-switch notification: the calendar is
    the system's most likely silent failure, so the two switch days a
    year get a human-verification prompt.
    """
    if d == last_sunday(d.year, 3):
        return "summer"
    if d == last_sunday(d.year, 10):
        return "winter"
    return None


def _effective_table(d: date, calendars: Calendars) -> CalendarTable:
    """Return the calendar table in force on a date."""
    table: CalendarTable | None = None
    for effective, candidate in calendars:
        if effective <= d:
            table = candidate
    if table is None:
        msg = f"No tariff calendar in force on {d}; earliest is {calendars[0][0]}"
        raise ValueError(msg)
    return table


def _day_class(d: date) -> DayClass:
    """Classify a date as weekday, saturday or sunday."""
    if d.weekday() == _SUNDAY:
        return "sunday"
    if d.weekday() == _SATURDAY:
        return "saturday"
    return "weekday"


def period(dt: datetime, calendars: Calendars = CALENDARS) -> Period:
    """
    Return the tariff period (ponta/cheias/vazio) for an instant.

    Naive datetimes are taken as Portugal legal time; aware datetimes
    are converted to Europe/Lisbon first.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_PORTUGAL)
    table = _effective_table(dt.date(), calendars)
    spans = table[season(dt.date())][_day_class(dt.date())]
    minute = dt.hour * 60 + dt.minute
    for start, end, name in spans:
        if start <= minute < end:
            return name
    msg = f"Calendar table does not cover minute {minute} of {dt.date()}"
    raise ValueError(msg)


def weekly_hours(
    season_name: Season,
    calendars: Calendars = CALENDARS,
    on: date | None = None,
) -> dict[Period, float]:
    """
    Total hours per period over one week (5 weekdays + Sat + Sun).

    Computed from the span table, not hardcoded. `on` selects the
    calendar version in force on that date; default is the latest.
    """
    table = _effective_table(on, calendars) if on else calendars[-1][1]
    totals: dict[Period, float] = {"ponta": 0.0, "cheias": 0.0, "vazio": 0.0}
    day_weights: dict[DayClass, int] = {"weekday": 5, "saturday": 1, "sunday": 1}
    for day_class, weight in day_weights.items():
        for start, end, name in table[season_name][day_class]:
            totals[name] += weight * (end - start) / 60
    return totals
