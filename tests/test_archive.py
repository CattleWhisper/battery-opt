"""
Tests for the daily price archive (archive.py, plan Task 10 decision 4).

Runs under pytest-homeassistant-custom-component: `hass.config.path()`
resolves into the plugin's `testing_config` dir. NOT a fresh tmp dir
per test, despite appearances — it is one fixed path reused across
every test *run* (`get_test_config_dir()` has no per-test uniqueness),
so files a test writes under it outlive that test and can leak into a
later run's "archive is empty" assertions. `_clean_archive_dir` below
wipes it before each test in this file for that reason (owner
2026-08-27, found while adding the recent-prices pool tests).
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_opt.archive import (
    ARCHIVE_SUBDIR,
    async_archive_day,
    async_recent_delivered_prices,
)
from custom_components.battery_opt.prices_source import DaySeries

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Lisbon")
DAY = date(2026, 7, 15)


@pytest.fixture(autouse=True)
def _clean_archive_dir(hass: HomeAssistant) -> None:
    """Start every test in this file with no pre-existing archive files."""
    shutil.rmtree(Path(hass.config.path(ARCHIVE_SUBDIR)), ignore_errors=True)


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


async def test_recent_prices_pools_the_trailing_days_excluding_day_itself(
    hass: HomeAssistant,
) -> None:
    """The pool covers `day - lookback_days .. day - 1`; never `day` itself."""
    for offset, price_eur_kwh in ((1, 0.10), (2, 0.20), (7, 0.70)):
        await async_archive_day(
            hass,
            DAY - timedelta(days=offset),
            _series(padded=False, price_eur_kwh=price_eur_kwh, omie_eur_mwh=1.0),
        )
    # Also archive `DAY` itself, and one day just past the lookback —
    # neither should show up in the pool.
    await async_archive_day(
        hass, DAY, _series(padded=False, price_eur_kwh=99.0, omie_eur_mwh=1.0)
    )
    await async_archive_day(
        hass,
        DAY - timedelta(days=8),
        _series(padded=False, price_eur_kwh=88.0, omie_eur_mwh=1.0),
    )

    pooled = await async_recent_delivered_prices(hass, DAY, lookback_days=7)

    assert 99.0 not in pooled
    assert 88.0 not in pooled
    assert len(pooled) == 3 * 96  # only 3 of the 7 lookback days were archived
    for expected in (0.10, 0.20, 0.70):
        assert sum(1 for p in pooled if p == pytest.approx(expected)) == 96


async def test_recent_prices_empty_when_no_archive_exists(
    hass: HomeAssistant,
) -> None:
    """A fresh install (or a `day` before the archive started) yields []."""
    assert await async_recent_delivered_prices(hass, DAY, lookback_days=7) == []


async def test_recent_prices_bounds_each_pooled_day(hass: HomeAssistant) -> None:
    """`first_quarter`/`last_quarter` slice EVERY pooled day, not just one."""
    series = DaySeries(
        omie_points=(),
        delivered_eur_kwh=tuple(float(i) for i in range(96)),
        padded=False,
    )
    await async_archive_day(hass, DAY - timedelta(days=1), series)
    await async_archive_day(hass, DAY - timedelta(days=2), series)

    pooled = await async_recent_delivered_prices(
        hass, DAY, lookback_days=7, first_quarter=90, last_quarter=96
    )

    assert pooled == [90.0, 91.0, 92.0, 93.0, 94.0, 95.0] * 2
