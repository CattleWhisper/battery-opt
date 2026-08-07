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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .archive import async_archive_day, async_archive_load_day
from .const import (
    BASE_LOAD_W,
    CONF_CAPACITY_KWH,
    CONF_LOAD_SENSOR,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_WEAR_COST,
    DEVICE_MAX_CHARGE_W,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .core.calendar import TZ_PORTUGAL
from .core.forecast import forecast_load
from .core.optimiser import solve
from .core.plan import (
    BatteryParams,
    saving_vs_no_cycling,
    soc_trajectory,
    validate_plan,
)
from .core.static_schedule import static_plan
from .driver import DriverError
from .load_history import LOOKBACK_DAYS, async_load_samples
from .prices_source import day_series_from_service

# A normal day; DST-short/long days (92/100) are a fallback-only edge
# case (no real prices to size the vector from) and are not material.
_INTERVALS_PER_DAY = 96

# HA core's OMIE integration exposes the day-ahead series through this
# service (sensors carry only the current price).
OMIE_SERVICE_DOMAIN = "omie"
OMIE_SERVICE_GET_PRICES = "get_prices_for_date"

# Plan Task 11, decision 7: MAE persists across restarts.
_MAE_STORE_VERSION = 1

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .core.forecast import DaySample
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
        self._load_mae_w: float | None = None
        self._mae_store: Store[dict[str, Any]] = Store(
            hass, _MAE_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_load_mae"
        )

    async def async_restore_load_mae(self) -> None:
        """Restore the persisted load-forecast MAE (decision 7), if any."""
        stored = await self._mae_store.async_load()
        if stored is not None:
            self._load_mae_w = stored.get("mae_w")

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
            # ADR-0007: planning C-3 capacity is the device limit; the
            # run-time contracted-power margin is the charge loop's.
            p_charge_max_w=DEVICE_MAX_CHARGE_W,
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
        series = await self._prices_for_day(today)
        prices = list(series.delivered_eur_kwh) if series is not None else None
        n = len(prices) if prices is not None else _INTERVALS_PER_DAY
        load = await self._forecast_load_vector(today, n)
        data.update(self._advisory_plan(params, data["soc_kwh"], prices, load))
        data["prices_eur_kwh"] = prices if data["prices_ok"] else None
        data["prices_padded"] = series.padded if series is not None else False
        data["load_mae_w"] = self._load_mae_w
        if series is not None:
            # Decision 4: archive every successful full-day build; the
            # same path is overwritten once a padded tail resolves.
            await async_archive_day(self.hass, today, series)
        data.update(await self._tomorrow_preview(today, params))
        return data

    async def _tomorrow_preview(
        self,
        today: date,
        params: BatteryParams,
    ) -> dict[str, Any]:
        """
        D+1 preview (decision 9): published only when D+1 itself builds.

        Tomorrow's own Lisbon day needs market dates D+1 and D+2; D+2
        is never published this far ahead, so tomorrow structurally
        always relies on the same tail-padding tolerance
        `_prices_for_day` already applies for today. Seeded at the
        reserve floor rather than chained from today's plan, since
        today has not finished executing yet — this is a speculative
        preview, not a committed plan.
        """
        empty: dict[str, Any] = {
            "tomorrow_prices_eur_kwh": None,
            "tomorrow_charge_w": None,
            "tomorrow_discharge_w": None,
        }
        tomorrow = today + timedelta(days=1)
        series = await self._prices_for_day(tomorrow)
        if series is None:
            return empty
        prices = list(series.delivered_eur_kwh)
        load = await self._forecast_load_vector(tomorrow, len(prices))
        solar = [0.0] * len(load)
        plan_params = dataclasses.replace(params, soc_start_kwh=params.cap_min_kwh)
        solve_params = dataclasses.replace(
            plan_params, wear_cost_eur_kwh=self.plan_wear_eur_kwh
        )
        result = solve(prices, load, solar, solve_params)
        if validate_plan(result.plan, load, solar, plan_params):
            # Fail closed like today's plan (decision 6's spirit): a
            # speculative preview that fails validation just doesn't
            # publish a plan, but the prices are still worth showing.
            return {**empty, "tomorrow_prices_eur_kwh": [round(p, 5) for p in prices]}
        return {
            "tomorrow_prices_eur_kwh": [round(p, 5) for p in prices],
            "tomorrow_charge_w": list(result.plan.charge_w),
            "tomorrow_discharge_w": list(result.plan.discharge_w),
        }

    async def _forecast_load_vector(self, today: date, n: int) -> list[float]:
        """
        Net load forecast (W) for advisory-plan input only (Task 11).

        Flat `BASE_LOAD_W` without a configured meter, or when history
        is too thin — `forecast_load` itself applies the <4-same-
        weekday-occurrences fallback per slot and for the whole day.
        """
        merged = {**self.entry.data, **self.entry.options}
        entity_id = merged.get(CONF_LOAD_SENSOR)
        if not entity_id:
            return [BASE_LOAD_W] * n
        samples: list[DaySample] = await async_load_samples(self.hass, entity_id, today)
        solar = [0.0] * n
        return forecast_load(today, samples, solar, n_intervals=n)

    async def _prices_for_day(self, day: date) -> DaySeries | None:
        """
        Price series for a Lisbon-local day from core OMIE.

        `omie.get_prices_for_date` needs market dates D and D+1 to
        cover a Lisbon day; a missing D+1 pads the final hour,
        flagged on the returned series. Shared by today's plan
        (decisions 4/6) and tomorrow's preview (decision 9).
        """
        if not self.hass.services.has_service(
            OMIE_SERVICE_DOMAIN, OMIE_SERVICE_GET_PRICES
        ):
            return None
        entries: list[dict[str, Any]] = []
        for offset in (0, 1):
            market_date = day + timedelta(days=offset)
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
        return day_series_from_service(day, entries)

    def _advisory_plan(
        self,
        params: BatteryParams,
        soc_kwh: float | None,
        prices: list[float] | None,
        load: list[float],
    ) -> dict[str, Any]:
        """Compute today's capped-greedy plan, or the static fallback."""
        today = dt_util.now().date()
        solar = [0.0] * len(load)
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
            "plan_soc_kwh": [
                round(v, 3) for v in soc_trajectory(result.plan, plan_params)
            ],
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
            "plan_soc_kwh": [
                round(v, 3) for v in soc_trajectory(fallback_plan, plan_params)
            ],
            "forecast_saving_eur": None,
            "vs_static_eur": None,
            "fallback": "static",
        }

    async def async_day_close(self, now: datetime) -> None:
        """
        Day close at 00:05 local (plan Task 11, decision 5).

        With a load meter configured: archive yesterday's OBSERVED
        load curve to `battery_opt/load/YYYY-MM-DD.json` (accumulating
        the future quarter-resolution forecast dataset) and compute
        the MAE of the forecast that *would have been* made for
        yesterday — using only history available before yesterday —
        against what was actually observed. Persisted so the MAE
        sensor survives restarts. Realised savings stays battery-
        gated and is intentionally not computed here. A no-op without
        a meter, or before yesterday has any observed data yet.
        """
        merged = {**self.entry.data, **self.entry.options}
        entity_id = merged.get(CONF_LOAD_SENSOR)
        if not entity_id:
            return
        today = now.date()
        yesterday = today - timedelta(days=1)
        observed_samples: list[DaySample] = await async_load_samples(
            self.hass, entity_id, today, lookback_days=1
        )
        observed = next((s for s in observed_samples if s.day == yesterday), None)
        if observed is None:
            _LOGGER.debug("Day close: no observed load for %s yet", yesterday)
            return
        await async_archive_load_day(self.hass, yesterday, observed)
        forecast_samples = await async_load_samples(
            self.hass, entity_id, yesterday, lookback_days=LOOKBACK_DAYS
        )
        forecast = forecast_load(
            yesterday, forecast_samples, [0.0] * len(observed.load_w)
        )
        errors = [
            abs(forecast_value - observed_value)
            for forecast_value, observed_value in zip(
                forecast, observed.load_w, strict=True
            )
            if observed_value is not None
        ]
        if not errors:
            _LOGGER.debug("Day close: no comparable slots for %s", yesterday)
            return
        mae = sum(errors) / len(errors)
        await self._mae_store.async_save(
            {"mae_w": mae, "computed_for": yesterday.isoformat()}
        )
        self._load_mae_w = mae
        await self.async_request_refresh()

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
        return "hold"

    def current_price_eur_kwh(self) -> float | None:
        """Delivered price (EDP formula) for the current quarter-hour."""
        data = self.data or {}
        prices = data.get("prices_eur_kwh")
        now = dt_util.now()
        if not prices or data.get("plan_date") != now.date():
            return None
        return prices[_quarter_index(now, len(prices))]

    def forecast_soc_kwh(self) -> float | None:
        """Advisory plan's SoC (kWh) at the end of the current quarter."""
        data = self.data or {}
        trajectory = data.get("plan_soc_kwh")
        now = dt_util.now()
        if not trajectory or data.get("plan_date") != now.date():
            return None
        # trajectory[i] is the SoC at the START of quarter i (97 values);
        # end of the current quarter = the next boundary.
        index = min(_quarter_index(now, len(trajectory) - 1) + 1, len(trajectory) - 1)
        return trajectory[index]


def _quarter_index(now: datetime, length: int) -> int:
    """
    Lisbon-local quarter-hour index into a day vector.

    The vectors are built on the Lisbon-local calendar day
    (prices_source._lisbon_date), so `now` must be converted to
    Europe/Lisbon before reading hour/minute — reading it in whatever
    zone `hass.config.time_zone` happens to be (dt_util.now()'s zone)
    silently picks a different day's or a shifted quarter-hour's slot
    whenever that isn't already Europe/Lisbon. Deliberately naive on
    the two DST days (92/100 quarters): off by at most one hour there,
    exact on every other day.
    """
    lisbon_now = now.astimezone(TZ_PORTUGAL)
    return min((lisbon_now.hour * 60 + lisbon_now.minute) // 15, length - 1)
