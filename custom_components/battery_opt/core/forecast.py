"""
Load forecast (plan Task 11): median of the last 4 same-weekday occurrences.

Pure function: no I/O, no clock reads, no homeassistant imports
(ADR-0001). The HA-side adapter that pulls recorder statistics lives
in `load_history.py`, outside `core/`, and is monkeypatched in
integration tests — this module is instead unit-tested exhaustively,
per the overnight-session decision to not spin up a real recorder in
tests.

Per-slot rule (owner 2026-08-27, forecast-accuracy hardening): for
each of the day's `n_intervals` quarter-hour slots, take the median
of that slot's value across whichever of the last `occurrences` (4)
same-weekday historical days actually have it — a slot needs only
`min_slot_samples` (2) present, not all `occurrences`, before it
falls back to `base_load_w`. A single recorder gap in one of the 4
days therefore degrades that one slot to a 3-sample median instead of
silently flattening it to base load; a slot missing from 3+ of the 4
days still falls back, since a median of 1 value is not a median. With
fewer than `occurrences` same-weekday days of ANY data at all
(including a fully-gapped day, which contributes no `DaySample`), the
WHOLE day is flat `base_load_w` (plan Task 11's "<4 weeks of data" —
same-weekday occurrences are a week apart, so 4 occurrences span 4
weeks) — this whole-day tier is unchanged; `load_history.LOOKBACK_DAYS`
widening the search window is what gives it margin against a single
missing historical day, not this module. Solar is subtracted as the
final step (net load, floored at zero), matching spec §6's
`net_load = max(0, house_load - solar)` — v1 always passes a zero
vector, but the signature carries it for when a solar forecast
exists.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from math import fsum
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
# Minimum present values a slot needs before it medians instead of
# falling back to base_load_w (owner 2026-08-27) — deliberately less
# than SAME_WEEKDAY_OCCURRENCES: the old all-or-nothing rule dropped a
# slot to flat load from a single missing hour in any one of the 4
# tracked days, which is the dominant source of forecast-vs-real SoC
# divergence (see CONTEXT.md's charge-energy-is-a-floor trap).
MIN_SLOT_SAMPLES = 2
# Same-day adaptive correction (owner 2026-08-27, Stage 3) tunables.
# 4 quarters = 1 h of today's own observations before engaging — long
# enough that one noisy 15-min reading cannot swing the ratio alone.
# The ratio itself is clamped to [MIN, MAX] so one atypical morning
# cannot scale the rest of the day's forecast — and therefore the
# rest of the day's PLAN — by more than a bounded factor either way.
MIN_ELAPSED_QUARTERS = 4
MIN_CORRECTION_RATIO = 0.3
MAX_CORRECTION_RATIO = 3.0
_EPS_W = 1e-6


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


# Keyword-only tunables (base load, interval count, lookback depth,
# per-slot minimum) are the Task 11 contract for exercising each
# fallback tier in tests and for a future quarter-resolution archive
# to override the defaults; the argument count is intentional, as in
# core/prices.py's price().
def forecast_load(  # noqa: PLR0913
    day: date,
    samples: Sequence[DaySample],
    solar_w: Sequence[float],
    *,
    base_load_w: float = BASE_LOAD_W,
    n_intervals: int = INTERVALS_PER_DAY,
    occurrences: int = SAME_WEEKDAY_OCCURRENCES,
    min_slot_samples: int = MIN_SLOT_SAMPLES,
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
            _slot_forecast(history, slot, min_slot_samples, base_load_w)
            for slot in range(n_intervals)
        ]
    return [max(0.0, load - solar) for load, solar in zip(raw, solar_w, strict=True)]


def _slot_forecast(
    history: list[DaySample],
    slot: int,
    min_slot_samples: int,
    base_load_w: float,
) -> float:
    values = [
        sample.load_w[slot]
        for sample in history
        if slot < len(sample.load_w) and sample.load_w[slot] is not None
    ]
    if len(values) < min_slot_samples:
        return base_load_w
    return statistics.median(values)


def merge_day_samples(
    primary: Sequence[DaySample],
    fallback: Sequence[DaySample],
) -> list[DaySample]:
    """
    Merge two DaySample sources, `primary` winning per date.

    Owner 2026-08-27 (forecast-accuracy hardening, Stage 2): lets a
    caller prefer one source of history over another on any date BOTH
    cover, while still using the other on a date only it has — e.g.
    `archive.async_recent_load_days`'s true quarter-resolution days
    (`primary`) over `load_history.py`'s hour-expanded ones
    (`fallback`) for the dates the archive has accumulated so far,
    with the hour-expanded adapter still covering the rest. Neither
    input order nor the output order is meaningful — `forecast_load`
    re-sorts and re-filters by same weekday internally.
    """
    by_day = {sample.day: sample for sample in fallback}
    by_day.update({sample.day: sample for sample in primary})
    return list(by_day.values())


def apply_intraday_correction(
    forecast_w: Sequence[float],
    observed_w: Sequence[float | None],
    *,
    min_elapsed_quarters: int = MIN_ELAPSED_QUARTERS,
    min_ratio: float = MIN_CORRECTION_RATIO,
    max_ratio: float = MAX_CORRECTION_RATIO,
) -> list[float]:
    """
    Scale the REST of today's forecast by how today has run so far.

    Owner 2026-08-27 (forecast-accuracy hardening, Stage 3): a median
    of same-weekday history is a "typical" day; nothing in
    `forecast_load` corrects for what is DIFFERENT about today. This
    closes that gap using the load meter alone — no SoC is read
    (ADR-0008 stays untouched).

    `observed_w[i]` is today's real load for quarter i where it has
    already happened, `None` from "now" onward — same convention as
    `DaySample.load_w`, so the HA-side caller can hand this function
    exactly what a same-day, lookback_days=1 sample gives it. The
    ratio `sum(observed at elapsed quarters) / sum(forecast at those
    SAME quarters)` is computed once, CLAMPED to
    `[min_ratio, max_ratio]`, and applied multiplicatively to every
    quarter that has NOT happened yet; already-elapsed quarters are
    returned exactly as forecast — this corrects what has not
    happened, it is not a substitute for the observation itself.

    Returns `forecast_w` unchanged (as a new list) below
    `min_elapsed_quarters` of real observations, or when the elapsed
    forecast sums to (near) zero — too little signal either way to
    correct responsibly.
    """
    if len(observed_w) != len(forecast_w):
        msg = f"observed_w has {len(observed_w)} entries, expected {len(forecast_w)}"
        raise ValueError(msg)
    elapsed = [i for i, v in enumerate(observed_w) if v is not None]
    if len(elapsed) < min_elapsed_quarters:
        return list(forecast_w)
    forecast_so_far = fsum(forecast_w[i] for i in elapsed)
    if forecast_so_far <= _EPS_W:
        return list(forecast_w)
    observed_so_far = fsum(observed_w[i] for i in elapsed)
    ratio = min(max_ratio, max(min_ratio, observed_so_far / forecast_so_far))
    return [
        forecast_w[i] if observed_w[i] is not None else forecast_w[i] * ratio
        for i in range(len(forecast_w))
    ]
