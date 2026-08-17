"""
Tests for `battery_opt.get_best_periods` and the best-periods sensor.

The daily-notification advisory: maximal contiguous cheap stretches
of a day's delivered prices, in time order. The OMIE stub shapes the
curve with one clearly cheapest 3 h stretch (negative spot — nothing
assumes prices are positive), so the defaults must report the WHOLE
stretch as one period, never a fixed-duration clip out of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from datetime import date

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)

# Frozen mid-week in winter (Lisbon = UTC): 2026-01-14 is a Wednesday.
_FROZEN_NOON = "2026-01-14T12:00:00+00:00"
_TZ_LISBON = ZoneInfo("Europe/Lisbon")


def _register_omie_stub(
    hass: HomeAssistant,
    days_available: int,
    cheap_day: date,
) -> None:
    """
    Stub core OMIE: flat 0.06 EUR/kWh spot, -0.25 on 13:00-16:00 Lisbon.

    The cheap stretch lands on `cheap_day` only; `days_available`
    market dates from today are served, requesting beyond raises like
    the real integration before publication.
    """
    cet = ZoneInfo("Europe/Madrid")
    first_served = dt_util.now().date()

    async def handler(call: ServiceCall) -> dict:
        market_date = call.data["date"]
        if (market_date - first_served).days >= days_available:
            msg = "data_not_available"
            raise ServiceValidationError(msg)
        midnight = datetime(
            market_date.year, market_date.month, market_date.day, tzinfo=cet
        )
        entries = []
        for i in range(96):
            start = midnight + timedelta(minutes=15 * i)
            local = start.astimezone(_TZ_LISBON)
            cheap = local.date() == cheap_day and 13 <= local.hour < 16
            entries.append(
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=15)).isoformat(),
                    "price": -0.25 if cheap else 0.06,
                }
            )
        return {"pt": entries}

    hass.services.async_register(
        "omie",
        "get_prices_for_date",
        handler,
        schema=_OMIE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a planning-only entry (registers the domain services too)."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _get_best_periods(hass: HomeAssistant, data: dict) -> dict:
    """Call the service and return its response."""
    response = await hass.services.async_call(
        DOMAIN,
        "get_best_periods",
        data,
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return dict(response)


async def test_defaults_report_the_whole_cheap_stretch(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The 3 h negative stretch comes out WHOLE — maximal, not clipped."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=2, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    response = await _get_best_periods(hass, {})

    assert response["day"] == "2026-01-14"
    assert response["prices_padded"] is False
    # Cheap cutoff = min + 20% of the range: still negative, so only
    # the negative-spot stretch qualifies.
    assert response["threshold_price_eur_kwh"] < 0
    periods = response["periods"]
    assert len(periods) == 1
    assert periods[0]["start"] == "2026-01-14T13:00:00+00:00"
    assert periods[0]["end"] == "2026-01-14T16:00:00+00:00"
    assert periods[0]["avg_price_eur_kwh"] < 0
    assert periods[0]["avg_price_eur_kwh"] < response["day_avg_price_eur_kwh"]


async def test_after_clips_the_stretch_and_judges_within_bounds(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stretch crossing `after` starts at the bound, not before it."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=2, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    response = await _get_best_periods(hass, {"after": "14:00"})

    periods = response["periods"]
    assert len(periods) == 1
    assert periods[0]["start"] == "2026-01-14T14:00:00+00:00"
    assert periods[0]["end"] == "2026-01-14T16:00:00+00:00"


async def test_min_duration_drops_too_short_stretches(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 3 h stretch disappears when at least 4 h are required."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=2, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    response = await _get_best_periods(hass, {"min_duration": "04:00:00"})

    assert response["periods"] == []


async def test_tomorrow_reports_tomorrows_prices(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """day: tomorrow uses the D+1 vector (tail-padded, as always)."""
    freezer.move_to(_FROZEN_NOON)
    tomorrow = dt_util.now().date() + timedelta(days=1)
    _register_omie_stub(hass, days_available=2, cheap_day=tomorrow)
    await _setup_entry(hass)

    response = await _get_best_periods(hass, {"day": "tomorrow"})

    assert response["day"] == "2026-01-15"
    assert response["prices_padded"] is True
    assert response["periods"][0]["start"] == "2026-01-15T13:00:00+00:00"
    assert response["periods"][0]["end"] == "2026-01-15T16:00:00+00:00"
    assert response["periods"][0]["avg_price_eur_kwh"] < 0


async def test_tomorrow_before_publication_raises(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No D+1 published yet: the call fails with a clear error."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=1, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    with pytest.raises(HomeAssistantError, match="tomorrow"):
        await _get_best_periods(hass, {"day": "tomorrow"})


async def test_best_periods_sensor_points_at_the_next_window(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """State = next not-yet-ended period; attributes carry both days."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=2, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    state = hass.states.get("sensor.battery_opt_best_periods")
    assert state is not None
    # Frozen at noon: the cheap stretch is still ahead.
    assert state.state == "2026-01-14T13:00:00+00:00"
    # Asymmetric on purpose (owner 2026-08-17): cheap stays selective,
    # expensive paints the top half — human steering, never planning.
    assert state.attributes["cheap_threshold_pct"] == 30.0
    assert state.attributes["expensive_threshold_pct"] == 50.0
    assert state.attributes["min_duration_minutes"] == 30
    periods = state.attributes["periods"]
    assert len(periods) == 1
    assert periods[0]["start"] == "2026-01-14T13:00:00+00:00"
    assert periods[0]["end"] == "2026-01-14T16:00:00+00:00"
    assert state.attributes["threshold_price_eur_kwh"] < 0
    assert state.attributes["day_avg_price_eur_kwh"] is not None
    # Tomorrow (flat spot): the TAR shape alone yields vazio periods.
    assert len(state.attributes["tomorrow_periods"]) >= 1
    # The mirrored red tier: the priciest stretch, clearly above the
    # cheap one, on both days.
    expensive = state.attributes["expensive_periods"]
    assert len(expensive) >= 1
    assert expensive[0]["avg_price_eur_kwh"] > periods[0]["avg_price_eur_kwh"]
    assert len(state.attributes["tomorrow_expensive_periods"]) >= 1

    # The sensor and the service agree — one ranking, two faces.
    response = await _get_best_periods(hass, {})
    assert response["periods"] == periods


async def test_best_periods_sensor_without_tomorrow(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No D+1 published: tomorrow's list is empty, today's stands."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=1, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    state = hass.states.get("sensor.battery_opt_best_periods")
    assert state is not None
    assert len(state.attributes["periods"]) == 1
    assert state.attributes["tomorrow_periods"] == []
    assert state.attributes["tomorrow_expensive_periods"] == []
    assert state.attributes["tomorrow_threshold_price_eur_kwh"] is None
    assert state.attributes["tomorrow_day_avg_price_eur_kwh"] is None


async def test_min_duration_that_does_not_fit_the_bounds_raises(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 2 h minimum cannot fit between 10:00 and 11:00."""
    freezer.move_to(_FROZEN_NOON)
    _register_omie_stub(hass, days_available=2, cheap_day=dt_util.now().date())
    await _setup_entry(hass)

    with pytest.raises(ServiceValidationError):
        await _get_best_periods(
            hass,
            {"after": "10:00", "before": "11:00", "min_duration": "02:00:00"},
        )
