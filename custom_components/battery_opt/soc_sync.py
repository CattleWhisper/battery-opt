"""
Midnight SoC sync (ADR-0010): the day's planning seed, measured once.

HA-side adapter — `core/` stays free of homeassistant imports
(ADR-0001). This is the ONLY place the integration reads the
battery's state of charge, and it exists for one job: seed the day's
plan from where the battery ACTUALLY is instead of where the model
believes it should be.

Why this does not reintroduce what ADR-0008 removed. That ADR deleted
an executor floor guard whose SoC source froze on sensor death,
leaving it "blind exactly when it matters" while looking protected.
Three things make this different:

1. **Planning only.** The value feeds the day's start SoC, nothing
   else. A wrong seed yields a suboptimal plan; it can never produce
   an unsafe action. Zero-export is still the firmware's anti-feed,
   the contracted-power ceiling is still the charge loop's, and the
   reserve floor is still the battery's (ADR-0008 stands on all
   three).
2. **Staleness is actually detected.** Freshness is judged on
   `last_reported`, which HA advances on every state write even when
   the percentage is unchanged — so a battery resting at a flat SoC
   overnight stays valid while a dead integration goes stale within
   `max_age`. This is precisely the check the old guard lacked.
3. **It fails to the model, not to a stale number.** Every failure
   path returns None and the caller keeps the existing modelled
   chain — the behaviour from before this module existed.

All-or-nothing across the fleet (ADR-0009): the seed is one aggregate
energy figure, so a partial read would under-seed the day and plan a
discharge the battery cannot deliver.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_PCT_MIN = 0.0
_PCT_MAX = 100.0


def within_sync_window(now: datetime, window_minutes: int) -> bool:
    """
    Report whether `now` is inside the post-midnight capture window.

    Outside it, "now" is no longer the day's START: a reading taken at
    14:00 says nothing about where the day began, and seeding with it
    would silently rewrite the morning. The caller then falls back to
    the modelled chain (or to a value already recorded for today).
    """
    return now.hour * 60 + now.minute < window_minutes


def measured_soc_kwh(
    hass: HomeAssistant,
    units: Sequence[tuple[str, float]],
    now: datetime,
    max_age_minutes: int,
) -> float | None:
    """
    Total measured fleet charge (kWh), or None if any unit is unusable.

    `units` pairs each SoC sensor with that unit's usable capacity, so
    a mixed-capacity fleet sums correctly. Returns None — never a
    partial or a guessed total — when any sensor is missing,
    unavailable, unparseable, out of the 0-100 range, or stale.
    """
    if not units:
        return None
    max_age = timedelta(minutes=max_age_minutes)
    total_kwh = 0.0
    for entity_id, capacity_kwh in units:
        state = hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            pct = float(state.state)
        except TypeError, ValueError:
            return None
        if not _PCT_MIN <= pct <= _PCT_MAX:
            return None
        # `last_reported` advances on every write, unchanged value
        # included — the distinction that makes a resting battery
        # valid and a dead integration stale (see the module docstring).
        reported = getattr(state, "last_reported", None) or state.last_updated
        if now - reported > max_age:
            return None
        total_kwh += pct / 100.0 * capacity_kwh
    return total_kwh
