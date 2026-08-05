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

from typing import TYPE_CHECKING, Any

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
