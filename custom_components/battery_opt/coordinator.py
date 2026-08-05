"""
Data update coordinator for battery_opt.

Task 8 scope: poll the battery SoC through the driver and expose the
effective BatteryParams built from the config entry. Planning, price
ingestion and actuation arrive with Tasks 9-12; nothing here blocks
the event loop — the driver reads entity states, no I/O of its own.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_WEAR_COST,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .core.plan import BatteryParams
from .driver import DriverError

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .driver import BatteryDriver


class BatteryOptCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll SoC and hold the effective parameters."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        driver: BatteryDriver,
    ) -> None:
        """Bind to the config entry and the battery driver."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self.driver = driver

    @property
    def battery_params(self) -> BatteryParams:
        """Effective parameters: entry data overlaid with options."""
        merged = {**self.entry.data, **self.entry.options}
        capacity = float(merged.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH))
        floor_pct = float(merged.get(CONF_RESERVE_FLOOR_PCT, DEFAULT_RESERVE_FLOOR_PCT))
        return BatteryParams(
            cap_usable_kwh=capacity,
            cap_min_kwh=capacity * floor_pct / 100.0,
            wear_cost_eur_kwh=float(merged.get(CONF_WEAR_COST, DEFAULT_WEAR_COST)),
        )

    @property
    def plan_wear_eur_kwh(self) -> float:
        """The cheias-cycling cap (Checkpoint B): optimiser planning wear."""
        merged = {**self.entry.data, **self.entry.options}
        return float(merged.get(CONF_PLAN_WEAR, DEFAULT_PLAN_WEAR))

    async def _async_update_data(self) -> dict[str, Any]:
        """Read the SoC; a driver failure marks the coordinator failed."""
        try:
            soc_percent = await self.driver.read_soc()
        except DriverError as err:
            msg = f"battery SoC unavailable: {err}"
            raise UpdateFailed(msg) from err
        params = self.battery_params
        return {
            "soc_percent": soc_percent,
            "soc_kwh": soc_percent / 100.0 * params.cap_usable_kwh,
        }
