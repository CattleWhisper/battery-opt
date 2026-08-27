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

`async_recent_delivered_prices` reads the price archive back: the
pooled multi-day reference the best-periods sensor and service size
their cheap/expensive cutoffs against (owner 2026-08-27), instead of
each day's own narrow spread. Missing or unreadable days are skipped,
not errors — a fresh install just returns an empty pool, and
`core.appliance`'s `reference_prices_eur_kwh` falls back to the
single-day range unchanged.

`async_recent_load_days` is the load archive's equivalent reader
(owner 2026-08-27, forecast-accuracy hardening, Stage 2) — decision
5's "supersedes the hourly recorder-statistics adapter … once enough
days have accumulated" had never actually been wired up; this is that
wiring. Unlike the price reader it returns `DaySample`s, not a flat
pool: `core.forecast.forecast_load` needs each day's own date to do
its same-weekday filtering, and `core.forecast.merge_day_samples`
is what lets a caller prefer these true quarter-resolution days over
`load_history.py`'s hour-expanded ones on any date both cover.

Thin and HA-side only: `core/` stays free of homeassistant imports
(ADR-0001). File IO runs off the event loop via
`hass.async_add_executor_job` (spec §9 boundary: no blocking I/O in
the coordinator).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .core.forecast import DaySample

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.core import HomeAssistant

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


def _read_delivered_prices(path: Path) -> list[float]:
    """Best-effort read; a missing or corrupt file yields no prices."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    values = payload.get("delivered_eur_kwh")
    return list(values) if isinstance(values, list) else []


async def async_recent_delivered_prices(
    hass: HomeAssistant,
    day: date,
    lookback_days: int,
    first_quarter: int = 0,
    last_quarter: int | None = None,
) -> list[float]:
    """
    Pool delivered-price quarters from the `lookback_days` days before `day`.

    Each day's own archived vector is bounded to [first_quarter,
    last_quarter) before pooling, same convention as
    `core.appliance.price_cutoff` — a caller that excludes the night
    via `after` gets a reference that excludes it too, on every
    pooled day, not just today's. Missing or unreadable days
    contribute nothing; the pool can come back empty (fresh install,
    or a `day` before the archive started).
    """
    pooled: list[float] = []
    for offset in range(1, lookback_days + 1):
        path = _archive_path(hass, day - timedelta(days=offset))
        values = await hass.async_add_executor_job(_read_delivered_prices, path)
        if not values:
            continue
        hi = len(values) if last_quarter is None else min(last_quarter, len(values))
        lo = min(first_quarter, hi)
        pooled.extend(values[lo:hi])
    return pooled


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


def _read_load_sample(path: Path, day: date) -> DaySample | None:
    """Best-effort read; a missing or corrupt file yields no sample."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    values = payload.get("load_w")
    if not isinstance(values, list):
        return None
    return DaySample(day=day, load_w=tuple(values))


async def async_recent_load_days(
    hass: HomeAssistant,
    day: date,
    lookback_days: int,
) -> list[DaySample]:
    """
    Return archived, TRUE quarter-resolution DaySamples strictly before `day`.

    One entry per day that has an archive file — order is not
    meaningful, `forecast_load` sorts and same-weekday-filters
    internally. Missing or unreadable days simply contribute nothing;
    the result can come back empty (fresh install, or a `day` before
    day-close archiving started). Pair with `core.forecast`'s
    `merge_day_samples` to prefer these over `load_history.py`'s
    hour-expanded samples on any date both cover.
    """
    samples: list[DaySample] = []
    for offset in range(1, lookback_days + 1):
        target = day - timedelta(days=offset)
        path = _load_archive_path(hass, target)
        sample = await hass.async_add_executor_job(_read_load_sample, path, target)
        if sample is not None:
            samples.append(sample)
    return samples
