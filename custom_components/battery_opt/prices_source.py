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

`day_series_from_service` is the richer contract (Task 10, decision
4): it also carries the raw OMIE EUR/MWh points with CET-aware
starts, which `archive.py` needs to write the daily price archive.
`day_price_vector_from_service` is kept as a thin wrapper over it for
callers that only need the delivered vector.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class DaySeries:
    """
    One Lisbon-local day assembled from core OMIE service entries.

    `omie_points` pairs each quarter's CET-aware start with the raw
    OMIE price in EUR/MWh, in the same order as `delivered_eur_kwh`.
    When `padded` is True, the tail entries in both are synthesised
    (start += 15 min, price repeats the last known value) — exactly
    what `archive.py` writes to disk.
    """

    omie_points: tuple[tuple[datetime, float], ...]
    delivered_eur_kwh: tuple[float, ...]
    padded: bool


def day_series_from_service(
    day: date,
    entries: list[dict[str, Any]],
) -> DaySeries | None:
    """
    Build a Lisbon day from core OMIE service entries.

    Market dates are CET days, so the Lisbon-local day D needs market
    D plus the first hour of D+1; before D+1 publishes (~13:30 CET)
    the missing tail is padded with the last known price, flagged via
    `DaySeries.padded`.

    Returns None when the day cannot be built at all.
    """
    day_points = [
        (datetime.fromisoformat(entry["start"]), float(entry["price"]) * 1000.0)
        for entry in entries
    ]
    local_points = sorted(
        (start, omie) for start, omie in day_points if _lisbon_date(start) == day
    )
    if not local_points:
        return None
    vector = [price(omie, start) for start, omie in local_points]
    # The day's true quarter count (92/96/100 across DST) from real
    # elapsed time — a plain count set cannot tell a DST-short day
    # from a normal day missing its tail.
    day_start = datetime(day.year, day.month, day.day, tzinfo=_TZ_LISBON)
    next_day = day_start + _ONE_DAY
    day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_TZ_LISBON)
    expected = round((day_end.timestamp() - day_start.timestamp()) / 900)
    if len(vector) == expected:
        return DaySeries(
            omie_points=tuple(local_points),
            delivered_eur_kwh=tuple(vector),
            padded=False,
        )
    missing = expected - len(vector)
    if 0 < missing <= _MAX_TAIL_PAD_QUARTERS:
        vector.extend([vector[-1]] * missing)
        last_start, last_omie = local_points[-1]
        padded_points = [
            *local_points,
            *(
                (last_start + timedelta(minutes=15 * step), last_omie)
                for step in range(1, missing + 1)
            ),
        ]
        return DaySeries(
            omie_points=tuple(padded_points),
            delivered_eur_kwh=tuple(vector),
            padded=True,
        )
    return None


def day_price_vector_from_service(
    day: date,
    entries: list[dict[str, Any]],
) -> tuple[list[float] | None, bool]:
    """Delivered EUR/kWh vector and tail_padded flag; see day_series_from_service."""
    series = day_series_from_service(day, entries)
    if series is None:
        return None, False
    return list(series.delivered_eur_kwh), series.padded


def _lisbon_date(start: datetime) -> date:
    return start.astimezone(_TZ_LISBON).date()
