"""
Daily price and load archives (plan Tasks 10 and 11).

One JSON file per Lisbon-local day at
`<config>/battery_opt/prices/YYYY-MM-DD.json`, written on every
successful full-day price build (padded or not) — when a padded
day's tail later resolves to real prices, the coordinator refreshes
and writes to the same path again, which is the overwrite. A second
archive at `<config>/battery_opt/load/YYYY-MM-DD.json` accumulates
one day per meter-configured day close, at quarter-hour resolution —
this is the future quarter-resolution forecast dataset that
supersedes the hourly recorder-statistics adapter (`load_history.py`)
once enough days have accumulated.

Thin and HA-side only: `core/` stays free of homeassistant imports
(ADR-0001). File IO runs off the event loop via
`hass.async_add_executor_job` (spec §9 boundary: no blocking I/O in
the coordinator).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.core import HomeAssistant

    from .core.forecast import DaySample
    from .prices_source import DaySeries

ARCHIVE_SUBDIR = "battery_opt/prices"
LOAD_ARCHIVE_SUBDIR = "battery_opt/load"


def _archive_path(hass: HomeAssistant, day: date) -> Path:
    """Return the archive file path for a given day."""
    return Path(hass.config.path(ARCHIVE_SUBDIR)) / f"{day.isoformat()}.json"


def _build_payload(day: date, series: DaySeries) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "fetched_at": dt_util.utcnow().isoformat(),
        "padded": series.padded,
        "omie_eur_mwh": [
            {"start": start.isoformat(), "price": omie_price}
            for start, omie_price in series.omie_points
        ],
        "delivered_eur_kwh": list(series.delivered_eur_kwh),
    }


def _write_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def async_archive_day(
    hass: HomeAssistant,
    day: date,
    series: DaySeries,
) -> None:
    """Write today's price series to the archive, overwriting in place."""
    path = _archive_path(hass, day)
    payload = _build_payload(day, series)
    await hass.async_add_executor_job(_write_file, path, payload)


def _load_archive_path(hass: HomeAssistant, day: date) -> Path:
    """Return the load-archive file path for a given day."""
    return Path(hass.config.path(LOAD_ARCHIVE_SUBDIR)) / f"{day.isoformat()}.json"


def _build_load_payload(day: date, sample: DaySample) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "archived_at": dt_util.utcnow().isoformat(),
        "load_w": list(sample.load_w),
    }


async def async_archive_load_day(
    hass: HomeAssistant,
    day: date,
    sample: DaySample,
) -> None:
    """Write yesterday's observed load curve to the archive (decision 5)."""
    path = _load_archive_path(hass, day)
    payload = _build_load_payload(day, sample)
    await hass.async_add_executor_job(_write_file, path, payload)
