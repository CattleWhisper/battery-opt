"""
Load forecast (plan Task 11): median of the last 4 same-weekday occurrences.

Pure function: no I/O, no clock reads, no homeassistant imports
(ADR-0001). The HA-side adapter that pulls recorder statistics lives
in `load_history.py`, outside `core/`, and is monkeypatched in
integration tests — this module is instead unit-tested exhaustively,
per the overnight-session decision to not spin up a real recorder in
tests.

Per-slot rule: for each of the day's `n_intervals` quarter-hour slots,
take the median of that slot's value across the last `occurrences`
(4) same-weekday historical days available; a slot with fewer than
`occurrences` such values falls back to `base_load_w`. With fewer
than `occurrences` same-weekday days of any data at all, the whole
day is flat `base_load_w` (plan Task 11's "<4 weeks of data" —
same-weekday occurrences are a week apart, so 4 occurrences span 4
weeks). Solar is subtracted as the final step (net load, floored at
zero), matching spec §6's `net_load = max(0, house_load - solar)` —
v1 always passes a zero vector, but the signature carries it for when
a solar forecast exists.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

# CONTEXT.md: flat 24/7 load; mirrors const.BASE_LOAD_W (kept as its
# own copy here, like the rest of core/, so core/ never reaches into
# the integration package for a constant).
BASE_LOAD_W = 1040.0
INTERVALS_PER_DAY = 96
SAME_WEEKDAY_OCCURRENCES = 4


@dataclass(frozen=True)
class DaySample:
    """
    One historical day's observed load, at quarter-hour resolution.

    `load_w[i]` is `None` where no observation exists for that slot
    (e.g. a recorder gap) — the per-slot median tolerates that
    instead of discarding an otherwise-usable day.
    """

    day: date
    load_w: tuple[float | None, ...]


# Keyword-only tunables (base load, interval count, lookback depth)
# are the Task 11 contract for exercising each fallback tier in tests
# and for a future quarter-resolution archive to override the
# defaults; the argument count is intentional, as in core/prices.py's
# price().
def forecast_load(  # noqa: PLR0913
    day: date,
    samples: Sequence[DaySample],
    solar_w: Sequence[float],
    *,
    base_load_w: float = BASE_LOAD_W,
    n_intervals: int = INTERVALS_PER_DAY,
    occurrences: int = SAME_WEEKDAY_OCCURRENCES,
) -> list[float]:
    """
    Forecast net load (W) for `day`'s `n_intervals` quarter-hours.

    `samples` need not be sorted or pre-filtered to same-weekday days
    — this function does both, keeping only the `occurrences` most
    recent same-weekday days strictly before `day`.
    """
    if len(solar_w) != n_intervals:
        msg = f"solar_w has {len(solar_w)} entries, expected {n_intervals}"
        raise ValueError(msg)
    history = sorted(
        (s for s in samples if s.day.weekday() == day.weekday() and s.day < day),
        key=lambda s: s.day,
        reverse=True,
    )[:occurrences]
    if len(history) < occurrences:
        raw = [base_load_w] * n_intervals
    else:
        raw = [
            _slot_forecast(history, slot, occurrences, base_load_w)
            for slot in range(n_intervals)
        ]
    return [max(0.0, load - solar) for load, solar in zip(raw, solar_w, strict=True)]


def _slot_forecast(
    history: list[DaySample],
    slot: int,
    occurrences: int,
    base_load_w: float,
) -> float:
    values = [
        sample.load_w[slot]
        for sample in history
        if slot < len(sample.load_w) and sample.load_w[slot] is not None
    ]
    if len(values) < occurrences:
        return base_load_w
    return statistics.median(values)
