"""
Coordinator-level tests for plan Task 11 (load forecast).

`core/forecast.py`'s pure function is unit-tested exhaustively in
test_forecast.py; per the overnight-session decision, the HA adapter
(`load_history.py`) is not exercised against a real recorder here —
these tests monkeypatch `coordinator.async_load_samples` instead,
covering: the coordinator falling back to a flat load without a
meter, using the forecast when one is configured, and the 00:05
day-close job (archive + MAE, decisions 5 and 7).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.archive import LOAD_ARCHIVE_SUBDIR
from custom_components.battery_opt.const import (
    BASE_LOAD_W,
    CONF_CAPACITY_KWH,
    CONF_LOAD_SENSOR,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)
from custom_components.battery_opt.core.forecast import DaySample
from custom_components.battery_opt.load_history import LOOKBACK_DAYS

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.core import HomeAssistant

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

_LOAD_SAMPLES_TARGET = "custom_components.battery_opt.coordinator.async_load_samples"


def _same_weekday_samples(
    anchor: date, watts: float, occurrences: int = 4
) -> list[DaySample]:
    """`occurrences` flat same-weekday days, one week apart, before `anchor`."""
    return [
        DaySample(day=anchor - timedelta(weeks=w), load_w=(watts,) * 96)
        for w in range(1, occurrences + 1)
    ]


async def test_forecast_load_vector_flat_without_meter(hass: HomeAssistant) -> None:
    """No CONF_LOAD_SENSOR configured -> flat BASE_LOAD_W, no adapter call."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    today = dt_util.now().date()
    with patch(_LOAD_SAMPLES_TARGET) as mock_loader:
        result = await coordinator._forecast_load_vector(today, 96)  # noqa: SLF001
    mock_loader.assert_not_called()
    assert result == [BASE_LOAD_W] * 96


async def test_forecast_load_vector_uses_history_when_meter_configured(
    hass: HomeAssistant,
) -> None:
    """A configured meter feeds core.forecast.forecast_load via the adapter."""
    today = dt_util.now().date()
    samples = _same_weekday_samples(today, watts=2000.0)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_LOAD_SENSOR: "sensor.house_power"}
    )
    entry.add_to_hass(hass)

    with patch(_LOAD_SAMPLES_TARGET, return_value=samples):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        result = await coordinator._forecast_load_vector(today, 96)  # noqa: SLF001

    assert result == [2000.0] * 96


async def test_advisory_plan_input_reflects_the_forecast(hass: HomeAssistant) -> None:
    """
    The advisory plan actually consumes the forecast, not just BASE_LOAD_W.

    A near-zero same-weekday history collapses the static fallback's
    ponta discharge toward zero net load, distinguishing it from the
    BASE_LOAD_W-flat default used without a meter.
    """
    today = dt_util.now().date()
    samples = _same_weekday_samples(today, watts=1.0)  # ~nothing to discharge
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_LOAD_SENSOR: "sensor.house_power"}
    )
    entry.add_to_hass(hass)

    with patch(_LOAD_SAMPLES_TARGET, return_value=samples):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    # No OMIE service registered -> static fallback plan, built from
    # this same forecast vector (coordinator.py's shared `load`).
    # Inclusive bound: with day-chaining the summer fallback carries
    # charge into ponta and discharges at exactly the 1 W net load
    # (zero-export) — still the forecast's cap, never BASE_LOAD_W's.
    assert coordinator.data["fallback"] == "static"
    assert max(coordinator.data["plan_discharge_w"]) <= 1.0


async def test_day_close_without_meter_is_a_no_op(hass: HomeAssistant) -> None:
    """No CONF_LOAD_SENSOR: day close does nothing, MAE stays unknown."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    with patch(_LOAD_SAMPLES_TARGET) as mock_loader:
        await coordinator.async_day_close(dt_util.now())
    mock_loader.assert_not_called()
    assert coordinator.data["load_mae_w"] is None
    assert hass.states.get("sensor.battery_opt_load_mae").state == "unknown"


async def test_day_close_archives_load_and_computes_mae(hass: HomeAssistant) -> None:
    """
    Decisions 5 and 7: day close archives yesterday and computes the MAE.

    The forecast for yesterday is built from history strictly before
    yesterday (1000 W flat); the observed curve is a flat 1200 W, so
    every slot's absolute error is 200 W and the MAE is exactly 200.
    """
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)
    forecast_samples = _same_weekday_samples(yesterday, watts=1000.0)
    observed_sample = DaySample(day=yesterday, load_w=(1200.0,) * 96)

    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_LOAD_SENSOR: "sensor.house_power"}
    )
    entry.add_to_hass(hass)

    async def fake_loader(
        _hass: HomeAssistant,
        _entity_id: str,
        day: date,
        lookback_days: int = LOOKBACK_DAYS,
    ) -> list[DaySample]:
        if day == today and lookback_days == 1:
            return [observed_sample]
        if day == yesterday and lookback_days == LOOKBACK_DAYS:
            return forecast_samples
        return []

    with patch(_LOAD_SAMPLES_TARGET, new=fake_loader):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_day_close(dt_util.now())
        await hass.async_block_till_done()

    assert coordinator.data["load_mae_w"] == pytest.approx(200.0)
    mae_state = hass.states.get("sensor.battery_opt_load_mae")
    assert float(mae_state.state) == pytest.approx(200.0)

    path = Path(hass.config.path(LOAD_ARCHIVE_SUBDIR)) / f"{yesterday.isoformat()}.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["date"] == yesterday.isoformat()
    assert payload["load_w"][0] == pytest.approx(1200.0)
    assert len(payload["load_w"]) == 96


async def test_day_close_skips_when_yesterday_has_no_observed_data(
    hass: HomeAssistant,
) -> None:
    """A meter configured but with no data yet: no crash, MAE stays unknown."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_LOAD_SENSOR: "sensor.house_power"}
    )
    entry.add_to_hass(hass)

    with patch(_LOAD_SAMPLES_TARGET, return_value=[]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_day_close(dt_util.now())

    assert coordinator.data["load_mae_w"] is None
    assert hass.states.get("sensor.battery_opt_load_mae").state == "unknown"


async def test_load_mae_persists_across_reload(hass: HomeAssistant) -> None:
    """Decision 7: the MAE survives a config-entry reload via the Store."""
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)
    forecast_samples = _same_weekday_samples(yesterday, watts=1000.0)
    observed_sample = DaySample(day=yesterday, load_w=(1200.0,) * 96)

    entry = MockConfigEntry(
        domain=DOMAIN, data={**PARAMETERS, CONF_LOAD_SENSOR: "sensor.house_power"}
    )
    entry.add_to_hass(hass)

    async def fake_loader(
        _hass: HomeAssistant,
        _entity_id: str,
        day: date,
        lookback_days: int = LOOKBACK_DAYS,
    ) -> list[DaySample]:
        if day == today and lookback_days == 1:
            return [observed_sample]
        if day == yesterday and lookback_days == LOOKBACK_DAYS:
            return forecast_samples
        return []

    with patch(_LOAD_SAMPLES_TARGET, new=fake_loader):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_day_close(dt_util.now())
        await hass.async_block_till_done()

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    mae_state = hass.states.get("sensor.battery_opt_load_mae")
    assert float(mae_state.state) == pytest.approx(200.0)
