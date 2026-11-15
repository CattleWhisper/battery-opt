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

ADR-0012: the delivered vector follows the configured `Tariff`. The
default (Indexada Horária, tri-horária, weekly) is bit-for-bit the
pre-ADR-0012 `price(omie, start)`. Indexada Média prices each group at
an ESTIMATE of the billing window's average OMIE (the archived
`omie_history` plus the day itself) and flags the series `estimated`.
A fixa contract needs no OMIE at all: `fixed_day_series` builds its
day from the contract prices alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .core.tariff import DEFAULT_TARIFF

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from .core.tariff import Tariff

_TZ_LISBON = ZoneInfo("Europe/Lisbon")
_ONE_DAY = timedelta(days=1)

_LOGGER = logging.getLogger(__name__)

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
    # Indexada Média: priced at an estimated window average (ADR-0012).
    estimated: bool = False


def day_series_from_service(
    day: date,
    entries: list[dict[str, Any]],
    tariff: Tariff = DEFAULT_TARIFF,
    omie_history: Sequence[tuple[datetime, float]] = (),
) -> DaySeries | None:
    """
    Build a Lisbon day from core OMIE service entries.

    Market dates are CET days, so the Lisbon-local day D needs market
    D plus the first hour of D+1; before D+1 publishes (~13:30 CET)
    the missing tail is padded with the last known price, flagged via
    `DaySeries.padded`.

    The series must be CONTIGUOUS from local midnight in exact 15-min
    steps: consumers index the vector positionally by quarter, so a
    head or mid-day gap would silently shift every later price one
    slot early. Any such gap (or a duplicate/off-grid start) is an
    error — logged, and the day is not built.

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
    # The day's true quarter count (92/96/100 across DST) from real
    # elapsed time — a plain count set cannot tell a DST-short day
    # from a normal day missing its tail.
    day_start = datetime(day.year, day.month, day.day, tzinfo=_TZ_LISBON)
    expected = _quarters_in(day)
    for index, (start, _omie) in enumerate(local_points):
        if round(start.timestamp() - day_start.timestamp()) != index * 900:
            _LOGGER.error(
                "OMIE series for %s is not contiguous: slot %d starts at %s, "
                "expected %s — refusing to build a misaligned price vector",
                day,
                index,
                start.isoformat(),
                (day_start + timedelta(seconds=index * 900)).isoformat(),
            )
            return None
    averages = None
    if tariff.pricing == "indexada_media":
        averages = tariff.period_averages([*omie_history, *local_points])
    vector: list[float] = [
        tariff.quarter_price(omie, start, averages)  # type: ignore[misc]
        for start, omie in local_points
    ]
    estimated = averages is not None
    if len(vector) == expected:
        return DaySeries(
            omie_points=tuple(local_points),
            delivered_eur_kwh=tuple(vector),
            padded=False,
            estimated=estimated,
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
            estimated=estimated,
        )
    return None


def fixed_day_series(day: date, tariff: Tariff) -> DaySeries:
    """
    Build a fixa contract's day: every quarter at its contract price, no OMIE.

    Always buildable (no publication to wait for), never padded, and
    carries no OMIE points — there is nothing market-side to archive.
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=_TZ_LISBON)
    origin = day_start.astimezone(UTC)
    vector = [
        tariff.fixed_price((origin + timedelta(minutes=15 * q)).astimezone(_TZ_LISBON))
        for q in range(_quarters_in(day))
    ]
    return DaySeries(omie_points=(), delivered_eur_kwh=tuple(vector), padded=False)


def _quarters_in(day: date) -> int:
    """Return the day's true quarter count (92/96/100 across DST)."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=_TZ_LISBON)
    next_day = day_start + _ONE_DAY
    day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=_TZ_LISBON)
    return round((day_end.timestamp() - day_start.timestamp()) / 900)


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
