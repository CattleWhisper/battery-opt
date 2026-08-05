"""
Data update coordinator for battery_opt.

Two modes, decided by whether the Marstek entities are configured:

- planning-only (battery not yet installed): no driver — each refresh
  pulls the day series from core OMIE's get_prices_for_date
  service, computes the advisory plan with
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

from .archive import async_archive_day
from .const import (
    BASE_LOAD_W,
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
from .core.optimiser import solve
from .core.plan import BatteryParams, saving_vs_no_cycling, validate_plan
from .core.static_schedule import static_plan
from .driver import DriverError
from .prices_source import day_series_from_service

# A normal day; DST-short/long days (92/100) are a fallback-only edge
# case (no real prices to size the vector from) and are not material.
_INTERVALS_PER_DAY = 96

# HA core's OMIE integration exposes the day-ahead series through this
# service (sensors carry only the current price).
OMIE_SERVICE_DOMAIN = "omie"
OMIE_SERVICE_GET_PRICES = "get_prices_for_date"

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .driver import BatteryDriver
    from .prices_source import DaySeries


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
        """Read the SoC (full mode), compute the advisory plan, archive."""
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
        today = dt_util.now().date()
        series = await self._today_prices()
        prices = list(series.delivered_eur_kwh) if series is not None else None
        data.update(self._advisory_plan(params, data["soc_kwh"], prices))
        data["prices_eur_kwh"] = prices if data["prices_ok"] else None
        data["prices_padded"] = series.padded if series is not None else False
        if series is not None:
            # Decision 4: archive every successful full-day build; the
            # same path is overwritten once a padded tail resolves.
            await async_archive_day(self.hass, today, series)
        return data

    async def _today_prices(self) -> DaySeries | None:
        """
        Today's price series from core OMIE.

        `omie.get_prices_for_date` needs market dates D and D+1 to
        cover a Lisbon day; a missing D+1 pads the final hour,
        flagged on the returned series.
        """
        today = dt_util.now().date()
        if not self.hass.services.has_service(
            OMIE_SERVICE_DOMAIN, OMIE_SERVICE_GET_PRICES
        ):
            return None
        entries: list[dict[str, Any]] = []
        for offset in (0, 1):
            market_date = today + timedelta(days=offset)
            try:
                response = await self.hass.services.async_call(
                    OMIE_SERVICE_DOMAIN,
                    OMIE_SERVICE_GET_PRICES,
                    {"date": market_date.isoformat(), "countries": ["pt"]},
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError as err:
                # D+1 is simply not published before ~13:30 CET.
                _LOGGER.debug("OMIE prices for %s: %s", market_date, err)
                continue
            payload = response or {}
            # Response keys are Country enum values ("pt"); tolerate a
            # future upstream switch back to uppercase codes.
            entries.extend(payload.get("pt") or payload.get("PT") or [])
        if not entries:
            return None
        return day_series_from_service(today, entries)

    def _advisory_plan(
        self,
        params: BatteryParams,
        soc_kwh: float | None,
        prices: list[float] | None,
    ) -> dict[str, Any]:
        """Compute today's capped-greedy plan, or the static fallback."""
        today = dt_util.now().date()
        n = len(prices) if prices is not None else _INTERVALS_PER_DAY
        load = [BASE_LOAD_W] * n
        solar = [0.0] * n
        # Virtual battery starts at the floor until a real SoC exists.
        start_soc = params.cap_min_kwh if soc_kwh is None else soc_kwh
        plan_params = dataclasses.replace(params, soc_start_kwh=start_soc)
        if prices is None:
            return self._static_fallback(today, load, solar, plan_params)
        solve_params = dataclasses.replace(
            plan_params, wear_cost_eur_kwh=self.plan_wear_eur_kwh
        )
        result = solve(prices, load, solar, solve_params)
        if validate_plan(result.plan, load, solar, plan_params):
            # Cannot happen by construction; fail closed (decision 6:
            # any untrustworthy dynamic plan falls back to static).
            return self._static_fallback(today, load, solar, plan_params)
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
            "fallback": None,
        }

    @staticmethod
    def _static_fallback(
        today: date,
        load: list[float],
        solar: list[float],
        plan_params: BatteryParams,
    ) -> dict[str, Any]:
        """
        No trustworthy dynamic plan: publish the static schedule instead.

        Decision 6: missing/failed prices (or, defensively, an invalid
        dynamic plan) still yield a valid, actuatable plan — the plan
        sensor's `fallback` attribute marks it. There is no price
        vector to cost it against, so the saving fields stay None.
        """
        fallback_plan = static_plan(today, load, solar, plan_params)
        return {
            "prices_ok": False,
            "plan_date": today,
            "plan_charge_w": list(fallback_plan.charge_w),
            "plan_discharge_w": list(fallback_plan.discharge_w),
            "forecast_saving_eur": None,
            "vs_static_eur": None,
            "fallback": "static",
        }

    def planned_action_now(self) -> str:
        """Return the advisory plan action for the current quarter-hour."""
        data = self.data or {}
        charge = data.get("plan_charge_w")
        discharge = data.get("plan_discharge_w")
        if not charge or data.get("plan_date") != dt_util.now().date():
            return "unknown"
        index = _quarter_index(dt_util.now(), len(charge))
        if charge[index] > 0:
            return "charge"
        if discharge[index] > 0:
            return "discharge"
        return "idle"

    def current_price_eur_kwh(self) -> float | None:
        """Delivered price (EDP formula) for the current quarter-hour."""
        data = self.data or {}
        prices = data.get("prices_eur_kwh")
        now = dt_util.now()
        if not prices or data.get("plan_date") != now.date():
            return None
        return prices[_quarter_index(now, len(prices))]


def _quarter_index(now: datetime, length: int) -> int:
    """
    Wall-clock quarter-hour index into a day vector.

    Deliberately naive on the two DST days (92/100 quarters): off by
    at most one hour there, exact on every other day.
    """
    return min((now.hour * 60 + now.minute) // 15, length - 1)
