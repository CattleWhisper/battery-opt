"""
Tests for the daily price archive (archive.py, plan Task 10 decision 4).

Runs under pytest-homeassistant-custom-component: `hass.config.path()`
already resolves into a throwaway test config directory, so no extra
tmp_path plumbing is needed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_opt.archive import ARCHIVE_SUBDIR, async_archive_day
from custom_components.battery_opt.prices_source import DaySeries

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 7, 15)


def _series(*, padded: bool, price_eur_kwh: float, omie_eur_mwh: float) -> DaySeries:
    start = datetime(2026, 7, 15, 0, 0, tzinfo=TZ)
    points = tuple((start + timedelta(minutes=15 * i), omie_eur_mwh) for i in range(96))
    return DaySeries(
        omie_points=points,
        delivered_eur_kwh=(price_eur_kwh,) * 96,
        padded=padded,
    )


def _archive_file(hass: HomeAssistant, day: date) -> Path:
    return Path(hass.config.path(ARCHIVE_SUBDIR)) / f"{day.isoformat()}.json"


async def test_archive_writes_expected_content(hass: HomeAssistant) -> None:
    """The file lands at the documented path with the documented shape."""
    series = _series(padded=False, price_eur_kwh=0.1534, omie_eur_mwh=62.5)
    await async_archive_day(hass, DAY, series)

    path = _archive_file(hass, DAY)
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["date"] == "2026-07-15"
    assert payload["padded"] is False
    assert "fetched_at" in payload
    assert len(payload["omie_eur_mwh"]) == 96
    assert payload["omie_eur_mwh"][0]["price"] == pytest.approx(62.5)
    assert payload["omie_eur_mwh"][0]["start"] == series.omie_points[0][0].isoformat()
    assert len(payload["delivered_eur_kwh"]) == 96
    assert payload["delivered_eur_kwh"][0] == pytest.approx(0.1534)


async def test_archive_overwrites_when_padding_resolves(hass: HomeAssistant) -> None:
    """A later, un-padded write for the same day replaces the padded one."""
    padded = _series(padded=True, price_eur_kwh=0.10, omie_eur_mwh=40.0)
    await async_archive_day(hass, DAY, padded)
    path = _archive_file(hass, DAY)
    assert json.loads(path.read_text())["padded"] is True

    full = _series(padded=False, price_eur_kwh=0.20, omie_eur_mwh=80.0)
    await async_archive_day(hass, DAY, full)

    payload = json.loads(path.read_text())
    assert payload["padded"] is False
    assert payload["delivered_eur_kwh"][0] == pytest.approx(0.20)
    assert payload["omie_eur_mwh"][0]["price"] == pytest.approx(80.0)
