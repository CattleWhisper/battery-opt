"""
Tests for the midnight SoC sync guards (ADR-0010, soc_sync.py).

The seed is the ONLY thing the integration reads SoC for, so these
tests are mostly about the ways a reading must be REFUSED: ADR-0008
removed the previous SoC path because it trusted a frozen sensor, and
every refusal here falls back to the modelled chain instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_opt.soc_sync import (
    measured_soc_kwh,
    within_sync_window,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Lisbon")
SOC = "sensor.battery_soc"
SOC_B = "sensor.battery_b_soc"
MAX_AGE = 30


def _now(hass: HomeAssistant, entity_id: str = SOC) -> datetime:
    """Return when the state was reported — i.e. a maximally fresh read."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.last_reported


async def test_reads_percent_against_capacity(hass: HomeAssistant) -> None:
    """44% of a 5 kWh unit is 2.2 kWh — the case that motivated ADR-0010."""
    hass.states.async_set(SOC, "44")
    assert measured_soc_kwh(hass, [(SOC, 5.0)], _now(hass), MAX_AGE) == pytest.approx(
        2.2
    )


async def test_fleet_sums_each_unit_against_its_own_capacity(
    hass: HomeAssistant,
) -> None:
    """ADR-0009: a mixed-capacity fleet must not average or assume."""
    hass.states.async_set(SOC, "50")
    hass.states.async_set(SOC_B, "25")
    total = measured_soc_kwh(hass, [(SOC, 5.0), (SOC_B, 10.0)], _now(hass), MAX_AGE)
    assert total == pytest.approx(2.5 + 2.5)


async def test_a_stale_reading_is_refused(hass: HomeAssistant) -> None:
    """
    The ADR-0008 failure mode, now actually detected.

    A dead integration stops reporting; once `last_reported` falls
    outside max_age the value is refused rather than trusted.
    """
    hass.states.async_set(SOC, "44")
    stale_now = _now(hass) + timedelta(minutes=MAX_AGE + 1)
    assert measured_soc_kwh(hass, [(SOC, 5.0)], stale_now, MAX_AGE) is None


async def test_an_unchanged_but_reported_value_stays_valid(
    hass: HomeAssistant,
) -> None:
    """
    A battery resting at a flat SoC overnight is NOT stale.

    This is why freshness rides `last_reported` and not `last_changed`
    — the charge does not move for hours at 100%, but the integration
    keeps reporting it.
    """
    hass.states.async_set(SOC, "100")
    first = _now(hass)
    hass.states.async_set(SOC, "100")  # same value, reported again
    assert _now(hass) >= first
    assert measured_soc_kwh(hass, [(SOC, 5.0)], _now(hass), MAX_AGE) == pytest.approx(
        5.0
    )


@pytest.mark.parametrize("value", ["unavailable", "unknown", "", "not-a-number"])
async def test_unusable_states_are_refused(hass: HomeAssistant, value: str) -> None:
    """Missing, unavailable and unparseable all fall back to the model."""
    hass.states.async_set(SOC, value)
    assert measured_soc_kwh(hass, [(SOC, 5.0)], _now(hass), MAX_AGE) is None


@pytest.mark.parametrize("value", ["-1", "101", "999"])
async def test_out_of_range_percentages_are_refused(
    hass: HomeAssistant, value: str
) -> None:
    """A percentage outside 0-100 is a broken sensor, not a real charge."""
    hass.states.async_set(SOC, value)
    assert measured_soc_kwh(hass, [(SOC, 5.0)], _now(hass), MAX_AGE) is None


async def test_a_missing_entity_is_refused(hass: HomeAssistant) -> None:
    """Nothing is guessed for a sensor that does not exist."""
    assert (
        measured_soc_kwh(
            hass,
            [("sensor.nope", 5.0)],
            datetime(2026, 8, 31, 0, 10, tzinfo=TZ),
            MAX_AGE,
        )
        is None
    )


async def test_one_bad_unit_refuses_the_whole_fleet(hass: HomeAssistant) -> None:
    """
    All-or-nothing: a partial total would under-seed the day.

    Seeding with 2 of 3 units' charge plans a discharge the battery
    cannot deliver, which is worse than keeping the model.
    """
    hass.states.async_set(SOC, "80")
    hass.states.async_set(SOC_B, "unavailable")
    assert (
        measured_soc_kwh(hass, [(SOC, 5.0), (SOC_B, 5.0)], _now(hass), MAX_AGE) is None
    )


async def test_no_units_is_refused(hass: HomeAssistant) -> None:
    """No configured sensor means no sync — the opt-in default."""
    assert measured_soc_kwh(hass, [], datetime(2026, 8, 31, tzinfo=TZ), MAX_AGE) is None


def test_sync_window_covers_only_just_after_midnight() -> None:
    """
    Past the window, "now" is no longer the day's START.

    Seeding an afternoon reading as the day's opening SoC would
    silently rewrite the morning that already happened.
    """
    assert within_sync_window(datetime(2026, 8, 31, 0, 0, tzinfo=TZ), 60)
    assert within_sync_window(datetime(2026, 8, 31, 0, 59, tzinfo=TZ), 60)
    assert not within_sync_window(datetime(2026, 8, 31, 1, 0, tzinfo=TZ), 60)
    assert not within_sync_window(datetime(2026, 8, 31, 14, 0, tzinfo=TZ), 60)
