"""
HA-side adapter: recorder long-term statistics -> core.forecast.DaySample.

Plan Task 11, decision 2 (overnight session): long-term statistics are
HOURLY, not quarter-hourly — short-term (5-minute) stats only survive
~10 days, too short for a 4-same-weekday-occurrences lookback. This is
a deliberate resolution deviation from the plan's quarter-hourly
wording; documented in docs/plan.md. Quarter resolution arrives later
from our own price/load archive (`archive.py`, decision 5) once it has
accumulated enough history.

Energy sensors (kWh) are converted to mean W via hourly `sum` deltas;
power sensors (W) use the hourly `mean` directly. Each hour is then
expanded into its 4 identical quarters, matching `core.forecast`'s
per-slot contract.

This module is deliberately thin and HA-only: `core/` stays free of
homeassistant imports (ADR-0001). It is not exercised against a real
recorder in tests — `coordinator.py` takes the loader as an injectable
callable so tests substitute a fake one; unit tests instead cover
`core.forecast.forecast_load` exhaustively.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.util import dt as dt_util

from .core.forecast import DaySample

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.core import HomeAssistant

LOOKBACK_DAYS = 28

_SensorKind = Literal["power", "energy"]

_POWER_UNITS = {UnitOfPower.WATT, UnitOfPower.KILO_WATT, UnitOfPower.MEGA_WATT}
_ENERGY_UNITS = {
    UnitOfEnergy.WATT_HOUR,
    UnitOfEnergy.KILO_WATT_HOUR,
    UnitOfEnergy.MEGA_WATT_HOUR,
}


def _sensor_kind(hass: HomeAssistant, entity_id: str) -> _SensorKind | None:
    """Power or energy, from the unit first, the device_class as fallback."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit in _POWER_UNITS:
        return "power"
    if unit in _ENERGY_UNITS:
        return "energy"
    device_class = state.attributes.get("device_class")
    if device_class == "power":
        return "power"
    if device_class == "energy":
        return "energy"
    return None


def _power_to_watts(value: float, unit: str | None) -> float:
    if unit == UnitOfPower.KILO_WATT:
        return value * 1_000.0
    if unit == UnitOfPower.MEGA_WATT:
        return value * 1_000_000.0
    return value  # already W; unrecognised units pass through best-effort


def _energy_delta_to_watts(delta_kwh_unit: float, unit: str | None) -> float:
    """Mean power over one hour, from that hour's energy consumption."""
    if unit == UnitOfEnergy.WATT_HOUR:
        delta_kwh = delta_kwh_unit / 1_000.0
    elif unit == UnitOfEnergy.MEGA_WATT_HOUR:
        delta_kwh = delta_kwh_unit * 1_000.0
    else:  # kWh, or an unrecognised unit treated as kWh best-effort
        delta_kwh = delta_kwh_unit
    # A negative delta is a meter reset, not negative consumption
    # (mirrors decision 8's cost-sensor rule) -> counts as 0.
    return max(0.0, delta_kwh * 1_000.0)


def _hourly_watts_power(
    rows: list[dict],
    unit: str | None,
) -> list[tuple[datetime, float | None]]:
    return [
        (
            dt_util.utc_from_timestamp(row["start"]),
            None if row["mean"] is None else _power_to_watts(row["mean"], unit),
        )
        for row in rows
    ]


def _hourly_watts_energy(
    rows: list[dict],
    unit: str | None,
) -> list[tuple[datetime, float | None]]:
    result: list[tuple[datetime, float | None]] = []
    previous: float | None = None
    for row in rows:
        current = row["sum"]
        start = dt_util.utc_from_timestamp(row["start"])
        if previous is None or current is None:
            result.append((start, None))
        else:
            result.append((start, _energy_delta_to_watts(current - previous, unit)))
        if current is not None:
            previous = current
    return result


def _group_into_day_samples(
    hourly: list[tuple[datetime, float | None]],
) -> list[DaySample]:
    """Bucket UTC hourly points into local days, expanding each hour x4."""
    by_day: dict[date, dict[int, float | None]] = defaultdict(dict)
    for start_utc, watts in hourly:
        local = dt_util.as_local(start_utc)
        by_day[local.date()][local.hour] = watts
    samples = []
    for day, hours in sorted(by_day.items()):
        slots: list[float | None] = []
        for hour in range(24):
            slots.extend([hours.get(hour)] * 4)
        samples.append(DaySample(day=day, load_w=tuple(slots)))
    return samples


async def async_load_samples(
    hass: HomeAssistant,
    entity_id: str,
    day: date,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[DaySample]:
    """
    Return up to `lookback_days` of historical DaySample, ending before `day`.

    Empty when the entity is unknown or its unit isn't recognised as
    power or energy — the coordinator then falls back to a flat load,
    same as having no meter configured at all.
    """
    kind = _sensor_kind(hass, entity_id)
    if kind is None:
        return []
    end = dt_util.start_of_local_day(day)
    start = end - timedelta(days=lookback_days)
    recorder = get_instance(hass)
    rows_by_id = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {entity_id},
        "hour",
        None,
        {"mean"} if kind == "power" else {"sum"},
    )
    rows = rows_by_id.get(entity_id, [])
    unit = hass.states.get(entity_id).attributes.get("unit_of_measurement")
    hourly = (
        _hourly_watts_power(rows, unit)
        if kind == "power"
        else _hourly_watts_energy(rows, unit)
    )
    return _group_into_day_samples(hourly)
