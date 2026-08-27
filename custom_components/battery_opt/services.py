"""
Domain services for battery_opt.

`battery_opt.get_best_periods` returns the cheap periods of a day's
delivered-price vector — the "run high-power appliances here"
advisory a daily notification automation templates from. Periods are
MAXIMAL contiguous cheap runs in time order (core/appliance.py holds
the semantics and why price alone is the right signal); price-only
and read-only: the plan and the battery are never touched. The cheap
cutoff is sized off the trailing week of archived prices, not the
requested day alone (owner 2026-08-27) — see `core.appliance`'s
`reference_prices_eur_kwh` and `archive.async_recent_delivered_prices`.

Registered from `async_setup`, so the service exists for the whole HA
run; the handler resolves the loaded config entry per call. Times use
the vectors' own wall-clock convention (core.plan segment convention:
Lisbon-local, deliberately naive on the two DST days, exact on every
other day).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .archive import async_recent_delivered_prices
from .const import (
    BEST_PERIODS_CHEAP_PCT,
    BEST_PERIODS_COUNT,
    BEST_PERIODS_LOOKBACK_DAYS,
    BEST_PERIODS_MIN_QUARTERS,
    DOMAIN,
)
from .core.appliance import cheap_periods, price_cutoff

if TYPE_CHECKING:
    from datetime import date, time
    from typing import Any

    from homeassistant.core import HomeAssistant, ServiceResponse

    from .coordinator import BatteryOptCoordinator

SERVICE_GET_BEST_PERIODS = "get_best_periods"

_QUARTER_MINUTES = 15

_GET_BEST_PERIODS_SCHEMA = vol.Schema(
    {
        vol.Optional("day", default="today"): vol.In(("today", "tomorrow")),
        vol.Optional(
            "min_duration",
            default=timedelta(minutes=_QUARTER_MINUTES * BEST_PERIODS_MIN_QUARTERS),
        ): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional("threshold", default=BEST_PERIODS_CHEAP_PCT): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional("count", default=BEST_PERIODS_COUNT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=12)
        ),
        vol.Optional("after"): cv.time,
        vol.Optional("before"): cv.time,
    }
)


def _quarter_of(moment: time) -> int:
    return (moment.hour * 60 + moment.minute) // _QUARTER_MINUTES


def _quarter_ceil(moment: time) -> int:
    minutes = moment.hour * 60 + moment.minute
    return -(-minutes // _QUARTER_MINUTES)


def _loaded_coordinator(hass: HomeAssistant) -> BatteryOptCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        msg = "battery_opt has no loaded config entry"
        raise ServiceValidationError(msg)
    return entries[0].runtime_data.coordinator


def _day_prices(
    coordinator: BatteryOptCoordinator,
    day_key: str,
) -> tuple[date, list[float], bool]:
    """
    Resolve the requested day's delivered-price vector.

    "today" needs the coordinator's current build (a stale plan_date
    right after midnight means the vector is still yesterday's);
    "tomorrow" additionally needs OMIE's D+1 publication (~13:30 CET).
    """
    data: dict[str, Any] = coordinator.data or {}
    today = dt_util.now().date()
    if data.get("plan_date") != today:
        msg = "today's prices are not built yet — retry after the next refresh"
        raise HomeAssistantError(msg)
    if day_key == "today":
        prices = data.get("prices_eur_kwh")
        padded = bool(data.get("prices_padded"))
        target = today
    else:
        prices = data.get("tomorrow_prices_eur_kwh")
        padded = bool(data.get("tomorrow_prices_padded"))
        target = today + timedelta(days=1)
    if not prices:
        msg = f"no delivered prices available for {day_key}"
        raise HomeAssistantError(msg)
    return target, list(prices), padded


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the battery_opt domain services."""

    async def _get_best_periods(call: ServiceCall) -> ServiceResponse:
        coordinator = _loaded_coordinator(hass)
        target, prices, padded = _day_prices(coordinator, call.data["day"])
        min_duration: timedelta = call.data["min_duration"]
        min_quarters = max(
            1, -(-int(min_duration.total_seconds()) // (_QUARTER_MINUTES * 60))
        )
        threshold_fraction = call.data["threshold"] / 100.0
        after: time | None = call.data.get("after")
        before: time | None = call.data.get("before")
        first_quarter = _quarter_ceil(after) if after is not None else 0
        last_quarter = _quarter_of(before) if before is not None else len(prices)
        if min(last_quarter, len(prices)) - first_quarter < min_quarters:
            msg = "min_duration does not fit between `after` and `before`"
            raise ServiceValidationError(msg)
        # Trailing-week HISTORY (owner 2026-08-27), anchored at TODAY
        # (never `target`) so it is always strictly-archived days —
        # requesting "tomorrow" must not pull today's still-live
        # vector in as tomorrow's history, or one freak swing today
        # would swamp tomorrow's own cutoff (see coordinator.py). The
        # window is bounded to the same [after, before) on every
        # pooled day, and unioned with THIS call's own bounded
        # segment, so an empty archive (fresh install) reduces to
        # exactly the old single-day cutoff.
        bound_hi = min(last_quarter, len(prices))
        reference = await async_recent_delivered_prices(
            hass,
            dt_util.now().date(),
            BEST_PERIODS_LOOKBACK_DAYS,
            first_quarter,
            bound_hi,
        )
        reference.extend(prices[first_quarter:bound_hi])
        cutoff = price_cutoff(
            prices,
            threshold_fraction,
            first_quarter,
            last_quarter,
            reference_prices_eur_kwh=reference,
        )
        return {
            "day": target.isoformat(),
            "prices_padded": padded,
            "day_avg_price_eur_kwh": round(sum(prices) / len(prices), 5),
            # The cheap cutoff actually used — "at or below this was
            # cheap today" for the notification, or a dashboard line.
            "threshold_price_eur_kwh": (None if cutoff is None else round(cutoff, 5)),
            "periods": cheap_periods(
                target,
                prices,
                threshold_fraction,
                min_quarters,
                call.data["count"],
                first_quarter=first_quarter,
                last_quarter=last_quarter,
                reference_prices_eur_kwh=reference,
            ),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_BEST_PERIODS,
        _get_best_periods,
        schema=_GET_BEST_PERIODS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
