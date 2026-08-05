"""
Data update coordinator for battery_opt.

Two modes, decided by whether the Marstek entities are configured:

- planning-only (battery not yet installed): no driver — each refresh
  reads the OMIE price sensor, computes the day's advisory plan with
  the capped greedy (plans at the plan-wear from the Checkpoint B
  decision, books savings at the true wear), and publishes plan +
  forecast saving + the vs-static delta. Nothing actuates. The
  virtual battery starts each day at the reserve floor.
- full: additionally polls the SoC through the driver; the executor
  (separate) actuates the static plan per Phase 1. The advisory plan
  is still computed — it is the dry-run the spec's Task 12 wants
  before dynamic actuation is ever enabled.

The greedy over 96 intervals is milliseconds — safe inline on the
event loop (ADR-0002); nothing here blocks.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    BASE_LOAD_W,
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_PRICE_SENSOR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_WEAR_COST,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .core.optimiser import solve
from .core.plan import BatteryParams, saving_vs_no_cycling, validate_plan
from .core.static_schedule import static_plan
from .driver import DriverError
from .prices_source import day_price_vector, day_price_vector_from_service

# HA core's OMIE integration exposes the day-ahead series through this
# service (sensors carry only the current price).
OMIE_SERVICE_DOMAIN = "omie"
OMIE_SERVICE_GET_PRICES = "get_prices_for_date"

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .driver import BatteryDriver


class BatteryOptCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll SoC (when a battery exists) and compute the advisory plan."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        driver: BatteryDriver | None,
    ) -> None:
        """Bind to the config entry; driver is None in planning-only."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self.driver = driver

    @property
    def planning_only(self) -> bool:
        """True while no battery is configured."""
        return self.driver is None

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
        """Read the SoC (full mode) and compute the advisory plan."""
        params = self.battery_params
        data: dict[str, Any] = {"soc_percent": None, "soc_kwh": None}
        if self.driver is not None:
            try:
                soc_percent = await self.driver.read_soc()
            except DriverError as err:
                msg = f"battery SoC unavailable: {err}"
                raise UpdateFailed(msg) from err
            data["soc_percent"] = soc_percent
            data["soc_kwh"] = soc_percent / 100.0 * params.cap_usable_kwh
        prices, padded = await self._today_prices()
        data.update(self._advisory_plan(params, data["soc_kwh"], prices))
        data["prices_padded"] = padded
        return data

    async def _today_prices(self) -> tuple[list[float] | None, bool]:
        """
        Today's delivered price vector from whichever OMIE exists.

        Tries the hass_omie attribute shape on the configured sensor
        first, then HA core's `omie.get_prices_for_date` service
        (which needs market dates D and D+1 to cover a Lisbon day; a
        missing D+1 pads the final hour, flagged in the return).
        """
        merged = {**self.entry.data, **self.entry.options}
        today = dt_util.now().date()
        price_state = self.hass.states.get(merged[CONF_PRICE_SENSOR])
        if price_state is not None:
            vector = day_price_vector(price_state.attributes, today)
            if vector is not None:
                return vector, False
        if self.hass.services.has_service(OMIE_SERVICE_DOMAIN, OMIE_SERVICE_GET_PRICES):
            entries: list[dict[str, Any]] = []
            for offset in (0, 1):
                market_date = today + timedelta(days=offset)
                try:
                    response = await self.hass.services.async_call(
                        OMIE_SERVICE_DOMAIN,
                        OMIE_SERVICE_GET_PRICES,
                        {"date": market_date.isoformat(), "countries": ["PT"]},
                        blocking=True,
                        return_response=True,
                    )
                except HomeAssistantError as err:
                    # D+1 is simply not published before ~13:30 CET.
                    _LOGGER.debug("OMIE prices for %s: %s", market_date, err)
                    continue
                entries.extend((response or {}).get("PT", []))
            if entries:
                return day_price_vector_from_service(today, entries)
        return None, False

    def _advisory_plan(
        self,
        params: BatteryParams,
        soc_kwh: float | None,
        prices: list[float] | None,
    ) -> dict[str, Any]:
        """Compute today's capped-greedy plan from the price vector."""
        empty = {
            "prices_ok": False,
            "plan_date": None,
            "plan_charge_w": None,
            "plan_discharge_w": None,
            "forecast_saving_eur": None,
            "vs_static_eur": None,
        }
        today = dt_util.now().date()
        if prices is None:
            return empty
        n = len(prices)
        load = [BASE_LOAD_W] * n
        solar = [0.0] * n
        # Virtual battery starts at the floor until a real SoC exists.
        start_soc = params.cap_min_kwh if soc_kwh is None else soc_kwh
        plan_params = dataclasses.replace(params, soc_start_kwh=start_soc)
        solve_params = dataclasses.replace(
            plan_params, wear_cost_eur_kwh=self.plan_wear_eur_kwh
        )
        result = solve(prices, load, solar, solve_params)
        if validate_plan(result.plan, load, solar, plan_params):
            return empty  # cannot happen by construction; fail closed
        static = static_plan(today, load, solar, plan_params)
        greedy_saving = saving_vs_no_cycling(result.plan, prices, plan_params)
        static_saving = saving_vs_no_cycling(static, prices, plan_params)
        return {
            "prices_ok": True,
            "plan_date": today,
            "plan_charge_w": list(result.plan.charge_w),
            "plan_discharge_w": list(result.plan.discharge_w),
            "forecast_saving_eur": round(greedy_saving, 4),
            "vs_static_eur": round(greedy_saving - static_saving, 4),
        }

    def planned_action_now(self) -> str:
        """Return the advisory plan action for the current quarter-hour."""
        data = self.data or {}
        charge = data.get("plan_charge_w")
        discharge = data.get("plan_discharge_w")
        if not charge or data.get("plan_date") != dt_util.now().date():
            return "unknown"
        now = dt_util.now()
        index = min((now.hour * 60 + now.minute) // 15, len(charge) - 1)
        if charge[index] > 0:
            return "charge"
        if discharge[index] > 0:
            return "discharge"
        return "idle"
