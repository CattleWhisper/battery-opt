"""
Data update coordinator for battery_opt.

Each refresh pulls the day series from core OMIE's
get_prices_for_date service, computes the advisory plan with the
capped greedy (plans at the plan-wear from the Checkpoint B decision,
books savings at the true wear), and publishes plan + forecast saving
+ the vs-static delta. Two virtual batteries, both chained (spec §8):
the STATIC baseline seeds per the actuated regime (the static chain
under dry-run — the battery's real morning state under Phase 1 — the
floor under dynamic), while the GREEDY chains ITS OWN persisted end
across days (today starts where yesterday's greedy ended; the regime
seed applies only when no yesterday record exists). No SoC is read
anywhere (owner decision 2026-08-07 — the floor is the battery's to
manage).
With the Marstek entities
configured, the executor (separate) additionally actuates: the static
plan while `dry_run` is on (the default — the advisory greedy is then
exactly the Task 12 dry-run), or, with `dry_run` off, the validated
greedy this coordinator publishes as `executor_plan` (static whenever
that is None).

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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .archive import async_archive_day, async_archive_load_day
from .const import (
    BASE_LOAD_W,
    CONF_CAPACITY_KWH,
    CONF_DRY_RUN,
    CONF_LOAD_SENSOR,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_SELF_DISCHARGE_W,
    CONF_WEAR_COST,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_DRY_RUN,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_SELF_DISCHARGE_W,
    DEFAULT_WEAR_COST,
    DEVICE_MAX_CHARGE_W,
    DEVICE_MAX_DISCHARGE_W,
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
from .core.static_schedule import chained_start_soc, static_plan
from .executor import DynamicDayPlan
from .fleet import battery_units
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

# Owner 2026-08-13: the greedy chains its own end across days; the
# recorded end survives restarts so today can seed from yesterday.
_GREEDY_END_STORE_VERSION = 1

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .core.forecast import DaySample
    from .driver import BatteryDriver
    from .prices_source import DaySeries


class BatteryOptCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Compute the advisory plan from core OMIE prices each refresh."""

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
        # Task 12: the day's validated greedy, published for the
        # executor (None while prices are missing or the solve is
        # untrustworthy — the executor then runs the chained static).
        self.executor_plan: DynamicDayPlan | None = None
        self._load_mae_w: float | None = None
        self._mae_store: Store[dict[str, Any]] = Store(
            hass, _MAE_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_load_mae"
        )
        # The most recent day a greedy was built: its date, the start
        # seed it was built FROM (stable across the day's refreshes)
        # and its planned end (tomorrow's seed). Persisted so the
        # greedy chain survives restarts.
        self._greedy_day: dict[str, Any] | None = None
        self._greedy_end_store: Store[dict[str, Any]] = Store(
            hass, _GREEDY_END_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_greedy_end"
        )

    async def async_restore_load_mae(self) -> None:
        """Restore the persisted load-forecast MAE (decision 7), if any."""
        stored = await self._mae_store.async_load()
        if stored is not None:
            self._load_mae_w = stored.get("mae_w")

    async def async_restore_greedy_end(self) -> None:
        """Restore the persisted greedy chain record, if any."""
        stored = await self._greedy_end_store.async_load()
        if stored is not None and stored.get("date"):
            self._greedy_day = stored

    def _record_greedy_day(self, day: date, start: float, end: float) -> None:
        record = {
            "date": day.isoformat(),
            "start_soc_kwh": round(start, 3),
            "end_soc_kwh": round(end, 3),
        }
        if record == self._greedy_day:
            return
        self._greedy_day = record
        self.hass.async_create_task(self._greedy_end_store.async_save(record))

    def _greedy_chain_seed(self, today: date) -> float | None:
        """
        Return the greedy's chained start for `today`, None if unchained.

        Yesterday's record chains its END forward; today's own record
        pins the START already used, so intraday refreshes never flip
        the seed. Anything older (first run, HA off yesterday,
        yesterday a static fallback) returns None — the regime default
        then applies.
        """
        if self._greedy_day is None:
            return None
        if self._greedy_day["date"] == today.isoformat():
            return float(self._greedy_day["start_soc_kwh"])
        if self._greedy_day["date"] == (today - timedelta(days=1)).isoformat():
            return float(self._greedy_day["end_soc_kwh"])
        return None

    @property
    def planning_only(self) -> bool:
        """True while no battery is configured."""
        return self.driver is None

    @property
    def battery_params(self) -> BatteryParams:
        """
        Effective parameters: ONE virtual battery over the fleet.

        ADR-0009: per-unit capacities and standby drains (battery
        subentries) sum; power limits are N x the device limit
        (planning C-3 still clamps to the house ceiling). With no
        units (planning-only, or a pre-migration flat group handled
        by `battery_units` itself) the parent-entry values and the
        defaults stand, as one virtual unit.
        """
        merged = {**self.entry.data, **self.entry.options}
        units = battery_units(self.entry)
        if units:
            capacity = sum(unit.capacity_kwh for unit in units)
            drain = sum(unit.self_discharge_w for unit in units)
            n = len(units)
        else:
            capacity = float(merged.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH))
            drain = float(merged.get(CONF_SELF_DISCHARGE_W, DEFAULT_SELF_DISCHARGE_W))
            n = 1
        floor_pct = float(merged.get(CONF_RESERVE_FLOOR_PCT, DEFAULT_RESERVE_FLOOR_PCT))
        return BatteryParams(
            cap_usable_kwh=capacity,
            cap_min_kwh=capacity * floor_pct / 100.0,
            wear_cost_eur_kwh=float(merged.get(CONF_WEAR_COST, DEFAULT_WEAR_COST)),
            # ADR-0007: planning C-3 capacity is the device limit; the
            # run-time contracted-power margin is the charge loop's.
            p_charge_max_w=DEVICE_MAX_CHARGE_W * n,
            p_discharge_max_w=DEVICE_MAX_DISCHARGE_W * n,
            # Owner 2026-08-17: measured standby drain; acts only on
            # the published trajectories and the chaining seeds.
            self_discharge_w=drain,
        )

    @property
    def plan_wear_eur_kwh(self) -> float:
        """The cheias-cycling cap (Checkpoint B): optimiser planning wear."""
        merged = {**self.entry.data, **self.entry.options}
        return float(merged.get(CONF_PLAN_WEAR, DEFAULT_PLAN_WEAR))

    @property
    def dry_run(self) -> bool:
        """Task 12 regime: True = the executor actuates the static plan."""
        merged = {**self.entry.data, **self.entry.options}
        return bool(merged.get(CONF_DRY_RUN, DEFAULT_DRY_RUN))

    def _day_start_soc(
        self,
        day: date,
        load: list[float],
        solar: list[float],
        params: BatteryParams,
    ) -> float:
        """
        Seed per the ACTUATED regime (Task 12 follow-up, owner 2026-08-13).

        Under dry-run the STATIC plan actuates, so every day starts at
        the previous weekday's planned static end (virtual
        day-chaining). With dry_run off the GREEDY actuates — and a
        greedy day ends at the reserve floor by construction (the
        single-day model values no stored energy: everything above the
        floor is sold wherever price beats wear) — so the seed is the
        floor, and the solve buys the night's cheap quarters before
        the morning ponta instead of assuming a full battery it will
        not have. (On extreme negative-price days the real end can sit
        above the floor; the battery then simply holds more than
        modelled — the safe direction, corrected within a day.)
        """
        if self.dry_run:
            return chained_start_soc(day, load, solar, params)
        return params.cap_min_kwh

    async def _async_update_data(self) -> dict[str, Any]:
        """Compute the advisory plan and archive the day's prices."""
        params = self.battery_params
        data: dict[str, Any] = {}
        today = dt_util.now().date()
        series = await self._prices_for_day(today)
        prices = list(series.delivered_eur_kwh) if series is not None else None
        n = len(prices) if prices is not None else _INTERVALS_PER_DAY
        load = await self._forecast_load_vector(today, n)
        data.update(self._advisory_plan(params, prices, load))
        data["prices_eur_kwh"] = prices if data["prices_ok"] else None
        data["prices_padded"] = series.padded if series is not None else False
        data["load_mae_w"] = self._load_mae_w
        if series is not None:
            # Decision 4: archive every successful full-day build; the
            # same path is overwritten once a padded tail resolves.
            await async_archive_day(self.hass, today, series)
        greedy_end = data["plan_soc_kwh"][-1] if data["fallback"] is None else None
        data.update(await self._tomorrow_preview(today, params, greedy_end))
        return data

    async def _tomorrow_preview(
        self,
        today: date,
        params: BatteryParams,
        today_greedy_end_kwh: float | None,
    ) -> dict[str, Any]:
        """
        D+1 preview (decision 9): published only when D+1 itself builds.

        Tomorrow's own Lisbon day needs market dates D+1 and D+2; D+2
        is never published this far ahead, so tomorrow structurally
        always relies on the same tail-padding tolerance
        `_prices_for_day` already applies for today.

        Seeds (owner 2026-08-13): the static baseline uses the regime
        seed (`_day_start_soc` — its own chain, so today's static end
        IS tomorrow's static start). Tomorrow's GREEDY chains from
        TODAY'S greedy planned end instead: the greedy line is one
        continuous trajectory, never a per-day counterfactual that
        "resets" at midnight — under dry-run today's greedy ends at
        the floor while the static chain would reseed tomorrow full,
        and the preview must show the overnight charge the greedy
        would actually plan from that low start. When today built no
        greedy (static fallback), the regime seed stands.
        """
        empty: dict[str, Any] = {
            "tomorrow_prices_eur_kwh": None,
            "tomorrow_prices_padded": None,
            "tomorrow_charge_w": None,
            "tomorrow_discharge_w": None,
            "tomorrow_static_charge_w": None,
            "tomorrow_static_discharge_w": None,
            "tomorrow_plan_soc_kwh": None,
            "tomorrow_static_soc_kwh": None,
        }
        tomorrow = today + timedelta(days=1)
        series = await self._prices_for_day(tomorrow)
        if series is None:
            return empty
        prices = list(series.delivered_eur_kwh)
        load = await self._forecast_load_vector(tomorrow, len(prices))
        solar = [0.0] * len(load)
        static_params = dataclasses.replace(
            params,
            soc_start_kwh=self._day_start_soc(tomorrow, load, solar, params),
        )
        # The static baseline needs no prices — published alongside the
        # greedy preview for the plan-comparison dashboard.
        static = static_plan(tomorrow, load, solar, static_params)
        greedy_params = dataclasses.replace(
            params,
            soc_start_kwh=(
                today_greedy_end_kwh
                if today_greedy_end_kwh is not None
                else self._day_start_soc(tomorrow, load, solar, params)
            ),
        )
        solve_params = dataclasses.replace(
            greedy_params, wear_cost_eur_kwh=self.plan_wear_eur_kwh
        )
        result = solve(prices, load, solar, solve_params)
        if validate_plan(result.plan, load, solar, greedy_params):
            # Fail closed like today's plan (decision 6's spirit): a
            # speculative preview that fails validation just doesn't
            # publish a plan, but the prices are still worth showing.
            return {
                **empty,
                "tomorrow_prices_eur_kwh": [round(p, 5) for p in prices],
                "tomorrow_prices_padded": series.padded,
                "tomorrow_static_charge_w": list(static.charge_w),
                "tomorrow_static_discharge_w": list(static.discharge_w),
                "tomorrow_static_soc_kwh": [
                    round(v, 3)
                    for v in soc_trajectory(
                        static, static_params, include_self_discharge=True
                    )
                ],
            }
        return {
            "tomorrow_prices_eur_kwh": [round(p, 5) for p in prices],
            "tomorrow_prices_padded": series.padded,
            "tomorrow_charge_w": list(result.plan.charge_w),
            "tomorrow_discharge_w": list(result.plan.discharge_w),
            "tomorrow_static_charge_w": list(static.charge_w),
            "tomorrow_static_discharge_w": list(static.discharge_w),
            "tomorrow_plan_soc_kwh": [
                round(v, 3)
                for v in soc_trajectory(
                    result.plan, greedy_params, include_self_discharge=True
                )
            ],
            "tomorrow_static_soc_kwh": [
                round(v, 3)
                for v in soc_trajectory(
                    static, static_params, include_self_discharge=True
                )
            ],
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
        prices: list[float] | None,
        load: list[float],
    ) -> dict[str, Any]:
        """Compute today's capped-greedy plan, or the static fallback."""
        today = dt_util.now().date()
        solar = [0.0] * len(load)
        # Two virtual batteries (owner 2026-08-13). The STATIC baseline
        # seeds per the actuated regime (_day_start_soc): dry-run
        # chains the static plan's own end — under static actuation
        # that IS the expected real morning SoC — and the dynamic
        # regime starts at the floor. The GREEDY chains ITS OWN end:
        # today starts at yesterday's recorded greedy end, falling back
        # to the regime seed only when no yesterday value exists (first
        # run, HA off yesterday, yesterday was a static fallback). One
        # continuous greedy trajectory, day after day — the same
        # convention the backtest chains both strategies under. Daily
        # savings keep that convention too: charge cost books on the
        # day it is bought, discharge revenue on the day it is sold
        # (energy carried in from yesterday is sunk-cost free energy).
        static_params = dataclasses.replace(
            params, soc_start_kwh=self._day_start_soc(today, load, solar, params)
        )
        if prices is None:
            self.executor_plan = None
            return self._static_fallback(today, load, solar, static_params)
        greedy_start = self._greedy_chain_seed(today)
        greedy_params = dataclasses.replace(
            params,
            soc_start_kwh=(
                greedy_start
                if greedy_start is not None
                else self._day_start_soc(today, load, solar, params)
            ),
        )
        solve_params = dataclasses.replace(
            greedy_params, wear_cost_eur_kwh=self.plan_wear_eur_kwh
        )
        result = solve(prices, load, solar, solve_params)
        if validate_plan(result.plan, load, solar, greedy_params):
            # Cannot happen by construction; fail closed (decision 6:
            # any untrustworthy dynamic plan falls back to static).
            self.executor_plan = None
            return self._static_fallback(today, load, solar, static_params)
        # Task 12: publish the validated greedy for the executor,
        # together with the exact inputs it was built with — the
        # executor re-validates each tick with these, never with its
        # own flat load vector.
        self.executor_plan = DynamicDayPlan(
            day=today,
            plan=result.plan,
            params=greedy_params,
            load_w=tuple(load),
            solar_w=tuple(solar),
        )
        # Published (and chained) trajectories carry the measured
        # standby drain; the validation above stayed flow-only.
        greedy_soc = soc_trajectory(
            result.plan, greedy_params, include_self_discharge=True
        )
        self._record_greedy_day(today, greedy_params.start_soc_kwh, greedy_soc[-1])
        static = static_plan(today, load, solar, static_params)
        greedy_saving = saving_vs_no_cycling(result.plan, prices, greedy_params)
        static_saving = saving_vs_no_cycling(static, prices, static_params)
        return {
            "prices_ok": True,
            "plan_date": today,
            "plan_charge_w": list(result.plan.charge_w),
            "plan_discharge_w": list(result.plan.discharge_w),
            "plan_soc_kwh": [round(v, 3) for v in greedy_soc],
            # The static baseline the vs_static delta is measured
            # against — published so dashboards can graph both plans
            # side by side (the Checkpoint C comparison view).
            "static_charge_w": list(static.charge_w),
            "static_discharge_w": list(static.discharge_w),
            "static_soc_kwh": [
                round(v, 3)
                for v in soc_trajectory(
                    static, static_params, include_self_discharge=True
                )
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
        # plan_params arrive day-chained from _advisory_plan, matching
        # the executor: the summer static plan only discharges if the
        # previous weekday's midday charge carries over.
        fallback_plan = static_plan(today, load, solar, plan_params)
        fallback_soc = [
            round(v, 3)
            for v in soc_trajectory(
                fallback_plan, plan_params, include_self_discharge=True
            )
        ]
        return {
            "prices_ok": False,
            "plan_date": today,
            "plan_charge_w": list(fallback_plan.charge_w),
            "plan_discharge_w": list(fallback_plan.discharge_w),
            "plan_soc_kwh": fallback_soc,
            # The published plan IS the static baseline here.
            "static_charge_w": list(fallback_plan.charge_w),
            "static_discharge_w": list(fallback_plan.discharge_w),
            "static_soc_kwh": fallback_soc,
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
        """
        Return the advisory plan action for the current quarter-hour.

        ADR-0007: the plan's power vectors are pure state selectors
        (`> 0`), matching the executor's mapping exactly — magnitudes
        belong to the closed loops, never to this display.
        """
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
