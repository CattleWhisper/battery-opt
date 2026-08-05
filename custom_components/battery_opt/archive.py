"""
Daily price archive (plan Task 10, decision 4).

One JSON file per Lisbon-local day at
`<config>/battery_opt/prices/YYYY-MM-DD.json`. Written on every
successful full-day price build (padded or not) — when a padded
day's tail later resolves to real prices, the coordinator refreshes
and writes to the same path again, which is the overwrite.

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

    from .prices_source import DaySeries

ARCHIVE_SUBDIR = "battery_opt/prices"


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
