"""
Integration tests for realised.py's RealisedTracker (plan Task 13).

Exercises the HA-side wiring against the bare `hass` fixture with a
duck-typed coordinator stub (price lookup, wear params, listener
registration) and freezegun stepping wall time: power-sample
integration, the stale-gap guard, day fold at rollover, the monthly
report notification with its >10% deviation flag, and restart
persistence via the Store.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.battery_opt.core.plan import BatteryParams
from custom_components.battery_opt.realised import MAX_SAMPLE_GAP_S, RealisedTracker

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

ENTITY_ID = "sensor.marstek_battery_power"


class _StubCoordinator:
    """Duck-types the pieces RealisedTracker uses from the coordinator."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.data: dict = {}
        self.price: float | None = 0.30
        self.listeners: list = []

    @property
    def battery_params(self) -> BatteryParams:
        return BatteryParams()  # wear 0.020

    def current_price_eur_kwh(self) -> float | None:
        return self.price

    def async_add_listener(self, listener) -> object:  # noqa: ANN001
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


def _set_power(hass: HomeAssistant, watts: float | str) -> None:
    hass.states.async_set(ENTITY_ID, str(watts), {"unit_of_measurement": "W"})


async def test_discharge_integrates_at_the_delivered_price(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """-4000 W held for 15 min at 0.30 books 1 kWh -> 0.28 net of wear."""
    freezer.move_to("2026-08-08T10:00:00+00:00")
    _set_power(hass, -4000.0)
    tracker = RealisedTracker(_StubCoordinator(hass), "entry1", ENTITY_ID)
    await tracker.async_start()

    freezer.tick(timedelta(minutes=15))  # within MAX_SAMPLE_GAP_S
    _set_power(hass, 0.0)
    await hass.async_block_till_done()

    assert tracker.state.discharged_kwh == pytest.approx(1.0)
    assert tracker.state.realised_eur == pytest.approx(0.30 - 0.020)
    tracker.async_stop()


async def test_charge_and_discharge_net_out(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A charge hour at 0.10 then a discharge hour at 0.30."""
    freezer.move_to("2026-08-08T10:00:00+00:00")
    _set_power(hass, 2000.0)
    stub = _StubCoordinator(hass)
    stub.price = 0.10
    tracker = RealisedTracker(stub, "entry1", ENTITY_ID)
    await tracker.async_start()

    freezer.tick(timedelta(minutes=15))
    _set_power(hass, -1000.0)  # closes the 0.5 kWh charge slice at 0.10
    await hass.async_block_till_done()
    stub.price = 0.30
    freezer.tick(timedelta(minutes=15))
    _set_power(hass, 0.0)  # closes the 0.25 kWh discharge slice at 0.30
    await hass.async_block_till_done()

    assert tracker.state.charged_kwh == pytest.approx(0.5)
    assert tracker.state.discharged_kwh == pytest.approx(0.25)
    assert tracker.state.charge_cost_eur == pytest.approx(0.5 * 0.10)
    assert tracker.state.discharge_value_eur == pytest.approx(0.25 * 0.30)
    tracker.async_stop()


async def test_stale_gap_contributes_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sample older than MAX_SAMPLE_GAP_S integrates as zero."""
    freezer.move_to("2026-08-08T10:00:00+00:00")
    _set_power(hass, -2000.0)
    tracker = RealisedTracker(_StubCoordinator(hass), "entry1", ENTITY_ID)
    await tracker.async_start()

    freezer.tick(timedelta(seconds=MAX_SAMPLE_GAP_S + 60))
    _set_power(hass, 0.0)
    await hass.async_block_till_done()

    assert tracker.state.discharged_kwh == 0.0
    tracker.async_stop()


async def test_day_folds_and_month_report_fires_with_deviation_flag(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Crossing the month boundary sends the reconciliation notification.

    Realised (measured) is far above the recorded forecast, so the
    report carries the >10% deviation flag (spec Task 13).
    """
    notifications = async_mock_service(hass, "persistent_notification", "create")
    # The bare hass fixture runs in US/Pacific: 22:00 UTC = 15:00 local.
    freezer.move_to("2026-08-31T22:00:00+00:00")
    _set_power(hass, -1000.0)
    stub = _StubCoordinator(hass)
    tracker = RealisedTracker(stub, "entry1", ENTITY_ID)
    await tracker.async_start()

    # The coordinator records a small forecast for the day.
    stub.data = {"plan_date": "2026-08-31", "forecast_saving_eur": 0.05}
    stub.listeners[0]()

    freezer.tick(timedelta(minutes=15))
    _set_power(hass, 0.0)  # 0.25 kWh discharged at 0.30 -> realised ~0.07
    await hass.async_block_till_done()

    freezer.move_to("2026-09-01T10:00:00+00:00")  # 03:00 local, September
    _set_power(hass, 100.0)  # any state change rolls day + month
    await hass.async_block_till_done()

    assert tracker.ledger.month == "2026-09"
    assert tracker.state.day == "2026-09-01"
    assert len(notifications) == 1
    message = notifications[0].data["message"]
    assert "2026-08" in notifications[0].data["title"]
    assert "ABOVE the 10%" in message
    tracker.async_stop()


async def test_restore_from_store_after_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A second tracker instance picks up the day and ledger."""
    freezer.move_to("2026-08-08T10:00:00+00:00")
    _set_power(hass, -4000.0)
    first = RealisedTracker(_StubCoordinator(hass), "entryX", ENTITY_ID)
    await first.async_start()
    freezer.tick(timedelta(minutes=15))
    _set_power(hass, 0.0)
    await hass.async_block_till_done()
    assert first.state.discharged_kwh == pytest.approx(1.0)
    first.async_stop()

    second = RealisedTracker(_StubCoordinator(hass), "entryX", ENTITY_ID)
    await second.async_start()
    assert second.state.day == first.state.day
    assert second.state.discharged_kwh == pytest.approx(1.0)
    second.async_stop()
