"""
Parse hass_omie sensor attributes into a day's delivered price vector.

Source of truth for the attribute shape: luuuis/hass_omie sensor.py
(verified 2026-08-05) — `today_hours` / `tomorrow_hours` are dicts of
LOCAL quarter-hour-start datetimes -> EUR/MWh spot prices; values can
be None while the day is provisional. This also settled the
production half of open question #1: the integration is quarter-
hourly.

HA-free module (the state attributes come in as plain Python
objects); delivered prices go through core.prices.price(), so K1 and
the TAR versioning apply exactly as in the backtest. Nothing here
assumes prices are positive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .core.prices import price

if TYPE_CHECKING:
    from datetime import date

# Accepted entry counts per local day: quarter-hourly (DST short/long
# included) or an hourly fallback that gets expanded x4 (costs ~2% of
# the saving — docs/findings.md, Task 6 side measurements).
_QUARTER_COUNTS = frozenset({92, 96, 100})
_HOURLY_COUNTS = frozenset({23, 24, 25})


def day_price_vector(
    attributes: dict[str, Any],
    day: date,
) -> list[float] | None:
    """
    Return the day's delivered EUR/kWh vector, or None if not ready.

    Searches both `today_hours` and `tomorrow_hours` (the wanted day
    shifts across them at midnight). A day counts as ready only when
    every interval is present and non-None.
    """
    entries: dict[Any, Any] = {}
    for key in ("today_hours", "tomorrow_hours"):
        hours = attributes.get(key)
        if isinstance(hours, dict):
            entries.update(hours)
    day_entries = sorted(
        (start, omie) for start, omie in entries.items() if start.date() == day
    )
    if not day_entries or any(omie is None for _, omie in day_entries):
        return None
    if len(day_entries) in _QUARTER_COUNTS:
        return [price(omie, start) for start, omie in day_entries]
    if len(day_entries) in _HOURLY_COUNTS:
        vector: list[float] = []
        for start, omie in day_entries:
            vector.extend([price(omie, start)] * 4)
        return vector
    return None


# Only the Lisbon day's final hour lives in the NEXT market date
# (published ~13:30 CET), so at most four tail quarters can be
# legitimately missing before then.
_MAX_TAIL_PAD_QUARTERS = 4


def day_price_vector_from_service(
    day: date,
    entries: list[dict[str, Any]],
) -> tuple[list[float] | None, bool]:
    """
    Build a Lisbon day from core OMIE service entries.

    The HA core `omie` integration's `get_prices_for_date` service
    returns [{"start": CET ISO datetime, "end": ..., "price": EUR/kWh
    (spot / 1000)}, ...] per market date (verified from
    home-assistant/core sources). Market dates are CET days, so the
    Lisbon-local day D needs market D plus the first hour of D+1;
    before D+1 publishes (~13:30 CET) the missing tail is padded with
    the last known price and flagged in the second return value.

    Returns (delivered EUR/kWh vector, tail_padded) — (None, False)
    when the day cannot be built.
    """
    day_points = [
        (datetime.fromisoformat(entry["start"]), float(entry["price"]) * 1000.0)
        for entry in entries
    ]
    local_points = sorted(
        (start, omie) for start, omie in day_points if _lisbon_date(start) == day
    )
    if not local_points:
        return None, False
    vector = [price(omie, start) for start, omie in local_points]
    # The day's true quarter count (92/96/100 across DST) from real
    # elapsed time — a plain count set cannot tell a DST-short day
    # from a normal day missing its tail.
    day_start = datetime(day.year, day.month, day.day, tzinfo=_TZ_LISBON)
    next_day = day_start + _ONE_DAY
    day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_TZ_LISBON)
    expected = round((day_end.timestamp() - day_start.timestamp()) / 900)
    if len(vector) == expected:
        return vector, False
    if 0 < expected - len(vector) <= _MAX_TAIL_PAD_QUARTERS:
        vector.extend([vector[-1]] * (expected - len(vector)))
        return vector, True
    return None, False


_TZ_LISBON = ZoneInfo("Europe/Lisbon")
_ONE_DAY = timedelta(days=1)


def _lisbon_date(start: datetime) -> date:
    return start.astimezone(_TZ_LISBON).date()
