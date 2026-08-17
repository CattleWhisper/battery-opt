"""
Find the cheap appliance-run windows of a delivered-price day.

Pure module: no I/O, no clock reads, no Home Assistant imports
(ADR-0001). This backs the `battery_opt.get_best_periods` service and
the best-periods sensor — "run high-power appliances here" — and is
deliberately price-only: the plan is not consulted. The delivered
price is the correct marginal signal for the extra grid energy an
appliance draws; battery coverage merely swaps it for vazio energy
plus wear, which the plan already prices in.

Windows are MAXIMAL by design (owner 2026-08-17): every contiguous
run of quarters at or below the day's cheap cutoff is reported whole
— "12:15-16:00", never a fixed-duration clip out of its middle — so
a short dip before a price spike surfaces too. The cutoff is relative
to the (bounded) range: min + threshold_fraction x (max - min). A
fraction of the SPREAD, not of the price level: it survives negative
prices (nothing here assumes prices are positive) and collapses
gracefully on a flat day, where everything is cheap and the whole
span is one window.

The EXPENSIVE mirror (`expensive_windows` / `expensive_periods`)
applies the same detection at the top of the range — maximal runs at
or above max - threshold x (max - min) — the "avoid these" tier of
the dashboard's traffic-light day strip. Everything between the two
tiers is the strip's middle band, computed by the card as the
complement.

Naming: these are appliance WINDOWS, never "periods" — `period` is
the TAR term (ponta/cheias/vazio) throughout this codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import fsum
from typing import TYPE_CHECKING

from .calendar import TZ_PORTUGAL

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, tzinfo


@dataclass(frozen=True)
class ApplianceWindow:
    """A contiguous run of quarters: [start, end) indices, mean price."""

    start_index: int
    end_index: int
    avg_price_eur_kwh: float


def _bounds(
    n: int,
    first_quarter: int,
    last_quarter: int | None,
) -> tuple[int, int]:
    lo = max(0, first_quarter)
    hi = n if last_quarter is None else min(last_quarter, n)
    return lo, hi


def price_cutoff(
    prices_eur_kwh: Sequence[float],
    threshold_fraction: float,
    first_quarter: int = 0,
    last_quarter: int | None = None,
) -> float | None:
    """
    Return the cheap cutoff for the (bounded) vector; None if empty.

    min + threshold_fraction x (max - min) over [first_quarter,
    last_quarter) — computed inside the bounds, so "from 08:00 on"
    ranks against what is actually reachable, not against a night
    minimum the caller excluded.
    """
    lo, hi = _bounds(len(prices_eur_kwh), first_quarter, last_quarter)
    if hi <= lo:
        return None
    segment = prices_eur_kwh[lo:hi]
    low = min(segment)
    return low + threshold_fraction * (max(segment) - low)


def cheap_windows(  # noqa: PLR0913
    prices_eur_kwh: Sequence[float],
    threshold_fraction: float,
    min_quarters: int,
    count: int,
    first_quarter: int = 0,
    last_quarter: int | None = None,
) -> list[ApplianceWindow]:
    """
    Return the maximal cheap runs, in time order.

    Every maximal run of consecutive quarters at or below
    `price_cutoff` becomes one window; runs shorter than
    `min_quarters` are dropped, and when more than `count` remain only
    the cheapest by average survive. The result stays chronological —
    a notification reads it in time order; each window carries its
    average price for ranking.
    """
    cutoff = price_cutoff(
        prices_eur_kwh, threshold_fraction, first_quarter, last_quarter
    )
    if cutoff is None or min_quarters < 1 or count < 1:
        return []
    lo, hi = _bounds(len(prices_eur_kwh), first_quarter, last_quarter)
    windows: list[ApplianceWindow] = []
    start: int | None = None
    for quarter in range(lo, hi + 1):
        cheap = quarter < hi and prices_eur_kwh[quarter] <= cutoff
        if cheap and start is None:
            start = quarter
        elif not cheap and start is not None:
            if quarter - start >= min_quarters:
                windows.append(
                    ApplianceWindow(
                        start_index=start,
                        end_index=quarter,
                        avg_price_eur_kwh=(
                            fsum(prices_eur_kwh[start:quarter]) / (quarter - start)
                        ),
                    )
                )
            start = None
    if len(windows) > count:
        cheapest = sorted(windows, key=lambda w: (w.avg_price_eur_kwh, w.start_index))[
            :count
        ]
        windows = sorted(cheapest, key=lambda w: w.start_index)
    return windows


def expensive_windows(  # noqa: PLR0913
    prices_eur_kwh: Sequence[float],
    threshold_fraction: float,
    min_quarters: int,
    count: int,
    first_quarter: int = 0,
    last_quarter: int | None = None,
) -> list[ApplianceWindow]:
    """
    Return the maximal expensive runs, in time order.

    The mirror of `cheap_windows`: runs at or above
    max - threshold x (max - min), the priciest kept under `count`.
    Implemented by negating the vector — one detection, both tiers.
    One deliberate asymmetry: a flat (zero-spread) day has NO
    expensive tier — the cheap detection honestly claims the whole
    span there ("any time is fine"), and the mirror must not claim
    it too.
    """
    lo, hi = _bounds(len(prices_eur_kwh), first_quarter, last_quarter)
    segment = prices_eur_kwh[lo:hi]
    if not segment or min(segment) == max(segment):
        return []
    mirrored = cheap_windows(
        [-price for price in prices_eur_kwh],
        threshold_fraction,
        min_quarters,
        count,
        first_quarter=first_quarter,
        last_quarter=last_quarter,
    )
    return [
        ApplianceWindow(
            start_index=window.start_index,
            end_index=window.end_index,
            avg_price_eur_kwh=-window.avg_price_eur_kwh,
        )
        for window in mirrored
    ]


def _as_periods(
    day: date,
    windows: Sequence[ApplianceWindow],
    tz: tzinfo,
    interval: timedelta,
) -> list[dict[str, str | float]]:
    """
    Render windows as display periods: ISO times, mean price.

    The one JSON shape the `get_best_periods` service response and the
    best-periods sensor attributes carry — in time order. Timestamps
    follow core.plan's segment convention (the vectors' own wall-clock
    localised to `tz`), so lists built for consecutive days
    concatenate into one multi-day list.
    """
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    return [
        {
            "start": (midnight + window.start_index * interval).isoformat(),
            "end": (midnight + window.end_index * interval).isoformat(),
            "avg_price_eur_kwh": round(window.avg_price_eur_kwh, 5),
        }
        for window in windows
    ]


def cheap_periods(  # noqa: PLR0913
    day: date,
    prices_eur_kwh: Sequence[float],
    threshold_fraction: float,
    min_quarters: int,
    count: int,
    first_quarter: int = 0,
    last_quarter: int | None = None,
    tz: tzinfo = TZ_PORTUGAL,
    interval: timedelta = timedelta(minutes=15),
) -> list[dict[str, str | float]]:
    """Return the cheap windows as display periods (see `_as_periods`)."""
    return _as_periods(
        day,
        cheap_windows(
            prices_eur_kwh,
            threshold_fraction,
            min_quarters,
            count,
            first_quarter=first_quarter,
            last_quarter=last_quarter,
        ),
        tz,
        interval,
    )


def expensive_periods(  # noqa: PLR0913
    day: date,
    prices_eur_kwh: Sequence[float],
    threshold_fraction: float,
    min_quarters: int,
    count: int,
    first_quarter: int = 0,
    last_quarter: int | None = None,
    tz: tzinfo = TZ_PORTUGAL,
    interval: timedelta = timedelta(minutes=15),
) -> list[dict[str, str | float]]:
    """Return the expensive windows as display periods (see `_as_periods`)."""
    return _as_periods(
        day,
        expensive_windows(
            prices_eur_kwh,
            threshold_fraction,
            min_quarters,
            count,
            first_quarter=first_quarter,
            last_quarter=last_quarter,
        ),
        tz,
        interval,
    )
