"""
Build a day's delivered price vector from HA core's OMIE integration.

The core `omie` integration (home-assistant/core, verified from
source) exposes the day-ahead series through the
`omie.get_prices_for_date` service: [{"start": CET ISO datetime,
"end": ..., "price": EUR/kWh (spot / 1000)}, ...] per market date.
Its sensors carry only the current price, so the service is the one
and only price source. Delivered prices go through
core.prices.price(), so K1 and the TAR versioning apply exactly as
in the backtest. Nothing here assumes prices are positive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .core.prices import price

if TYPE_CHECKING:
    from datetime import date

_TZ_LISBON = ZoneInfo("Europe/Lisbon")
_ONE_DAY = timedelta(days=1)

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

    Market dates are CET days, so the Lisbon-local day D needs market
    D plus the first hour of D+1; before D+1 publishes (~13:30 CET)
    the missing tail is padded with the last known price and flagged
    in the second return value.

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


def _lisbon_date(start: datetime) -> date:
    return start.astimezone(_TZ_LISBON).date()
