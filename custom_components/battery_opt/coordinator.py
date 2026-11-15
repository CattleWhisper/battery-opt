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

UPS mode (ADR-0011, `self.ups_mode`) replaces the greedy entirely
while it is on: today's plan and tomorrow's preview both become
charge-cheapest/never-discharge plans, the static baseline is still
published beside them so `vs_static_eur` prices what the arbitrage
is giving up, and the greedy chain record is left untouched.

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

from .archive import (
    async_archive_day,
    async_archive_load_day,
    async_recent_delivered_prices,
    async_recent_load_days,
    async_recent_omie_points,
)
from .const import (
    BASE_LOAD_W,
    BEST_PERIODS_LOOKBACK_DAYS,
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
    MEDIA_LOOKBACK_DAYS,
    SOC_SYNC_MAX_AGE_MINUTES,
    SOC_SYNC_WINDOW_MINUTES,
    UPDATE_INTERVAL_MINUTES,
)
from .core.calendar import TZ_PORTUGAL
from .core.forecast import apply_intraday_correction, forecast_load, merge_day_samples
from .core.optimiser import solve
from .core.plan import (
    BatteryParams,
    Plan,
    saving_vs_no_cycling,
    soc_trajectory,
    validate_plan,
)
from .core.static_schedule import chained_start_soc, static_plan
from .core.ups import ups_plan, ups_start_soc_kwh
from .executor import DynamicDayPlan
from .fleet import battery_units, fleet_soc_sensors
from .load_history import LOOKBACK_DAYS, async_load_samples
from .prices_source import day_series_from_service, fixed_day_series
from .soc_sync import measured_soc_kwh, within_sync_window
from .tariff_config import tariff_from_config

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

# ADR-0010: the measured midnight seed, persisted so a restart later
# in the day keeps today's synced start instead of silently reverting
# to the modelled chain (the capture window is long gone by then).
_MIDNIGHT_SOC_STORE_VERSION = 1

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .core.forecast import DaySample
    from .core.tariff import Tariff
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
        # ADR-0011: set by switch.battery_opt_ups_mode. While on, every
        # plan this coordinator builds charges on the cheapest energy
        # and never discharges. Deliberately NOT a config option: it is
        # the switch's single source of truth, and the executor reads
        # it from here so the two can never disagree.
        self.ups_mode = False
        # ADR-0012: the supply contract. Fixed for the entry's lifetime
        # (every option change reloads the entry).
        self.tariff: Tariff = tariff_from_config({**entry.data, **entry.options})
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
        # ADR-0010: {"date", "soc_kwh"} for the day whose start SoC was
        # measured. One capture per day, inside the post-midnight
        # window; None until the first successful sync.
        self._midnight_soc: dict[str, Any] | None = None
        self._midnight_soc_store: Store[dict[str, Any]] = Store(
            hass, _MIDNIGHT_SOC_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_midnight_soc"
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

    async def async_restore_midnight_soc(self) -> None:
        """Restore today's measured seed (ADR-0010), if one was captured."""
        stored = await self._midnight_soc_store.async_load()
        if stored is not None and stored.get("date"):
            self._midnight_soc = stored

    def measured_start_soc_kwh(self, day: date) -> float | None:
        """
        Return the day's measured start SoC (kWh), or None to use the model.

        ADR-0010. Captured at most once per day: inside the
        post-midnight window a fresh reading is taken and recorded;
        afterwards only that record is served, so a restart at 14:00
        keeps the morning's synced seed rather than seeding the day
        with an afternoon SoC. Every failure path — no sensor, a
        partial fleet, a stale or implausible reading, or simply
        being past the window with nothing recorded — returns None,
        and the caller keeps the modelled chain unchanged.
        """
        recorded = self._midnight_soc
        if recorded is not None and recorded.get("date") == day.isoformat():
            return float(recorded["soc_kwh"])
        now = dt_util.now()
        if day != now.date() or not within_sync_window(now, SOC_SYNC_WINDOW_MINUTES):
            return None
        sensors = fleet_soc_sensors(self.entry)
        if not sensors:
            return None
        units = battery_units(self.entry)
        params = self.battery_params
        # Pair each sensor with ITS unit's capacity so a mixed fleet
        # sums correctly; the legacy single-group entry has no units,
        # and then the whole virtual battery is the one capacity.
        capacities = (
            [unit.capacity_kwh for unit in units]
            if len(units) == len(sensors)
            else [params.cap_usable_kwh]
        )
        measured = measured_soc_kwh(
            self.hass,
            list(zip(sensors, capacities, strict=True)),
            now,
            SOC_SYNC_MAX_AGE_MINUTES,
        )
        if measured is None:
            return None
        # C-4/C-5 are plan properties; the seed itself must sit inside
        # the modelled envelope or every trajectory built from it
        # starts out of bounds.
        seed = min(max(measured, params.cap_min_kwh), params.cap_usable_kwh)
        record = {"date": day.isoformat(), "soc_kwh": round(seed, 3)}
        self._midnight_soc = record
        self.hass.async_create_task(self._midnight_soc_store.async_save(record))
        _LOGGER.debug("Midnight SoC sync for %s: %.3f kWh", day, seed)
        return seed

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

        Today's own record pins the START already used, so intraday
        refreshes never flip the seed. Failing that, ADR-0010's
        measured midnight seed wins over yesterday's END: both answer
        "where is the battery now", and one of them actually looked.
        Yesterday's modelled end chains forward only when no
        measurement exists. Anything older (first run, HA off
        yesterday, yesterday a static fallback) returns None — the
        regime default then applies.
        """
        if (
            self._greedy_day is not None
            and self._greedy_day["date"] == today.isoformat()
        ):
            return float(self._greedy_day["start_soc_kwh"])
        measured = self.measured_start_soc_kwh(today)
        if measured is not None:
            return measured
        if (
            self._greedy_day is not None
            and self._greedy_day["date"] == (today - timedelta(days=1)).isoformat()
        ):
            return float(self._greedy_day["end_soc_kwh"])
        return None

    @property
    def ups_active(self) -> bool:
        """
        True when today's plans are UPS plans (ADR-0011, ADR-0012).

        Either the owner turned the switch on, or the contract is flat
        (fixa simples, Média simples): with no cheaper quarter to buy
        in, there is nothing to arbitrage, and holding the pack as
        backup is the only use left. The switch keeps its own state
        either way — a flat tariff adds a reason, it never overrides
        the owner's choice — so changing tariff restores it intact.
        """
        return self.ups_mode or self.tariff.is_flat

    @property
    def ups_reason(self) -> str | None:
        """Why UPS is active: `switch`, `tariff`, or None when it is not."""
        if self.ups_mode:
            return "switch"
        if self.tariff.is_flat:
            return "tariff"
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
        measured = self.measured_start_soc_kwh(day)
        if measured is not None:
            # ADR-0010: where the battery ACTUALLY is beats where the
            # chain thinks it should be. This is what lets the plan
            # spend a surplus it really has instead of stopping the
            # ponta discharge on modelled-empty.
            return measured
        if self.dry_run:
            return chained_start_soc(day, load, solar, params, tariff=self.tariff)
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
        data["prices_estimated"] = series.estimated if series is not None else False
        data["load_mae_w"] = self._load_mae_w
        data["tariff"] = self.tariff.key
        data["ups_reason"] = self.ups_reason if self.ups_active else None
        if series is not None:
            # Decision 4: archive every successful full-day build; the
            # same path is overwritten once a padded tail resolves.
            await async_archive_day(self.hass, today, series, self.tariff.key)
        # Best-periods history (owner 2026-08-27): the trailing
        # BEST_PERIODS_LOOKBACK_DAYS days of ARCHIVED prices, strictly
        # before today — `async_recent_delivered_prices` starts at
        # offset 1, so today itself is never in it. Deliberately NOT
        # unioned with today's own vector here: the sensor combines
        # this shared history with EACH day's own vector separately
        # (today's with today's, tomorrow's with tomorrow's) so one
        # day's freak price swing widens that day's own cutoff without
        # ever leaking into a neighbouring day's classification — a
        # fresh install with no archive yet leaves this empty, which
        # reduces every day to its own range (core.appliance's
        # fallback) exactly as before this feature existed.
        data["best_periods_history_eur_kwh"] = await async_recent_delivered_prices(
            self.hass,
            today,
            BEST_PERIODS_LOOKBACK_DAYS,
            tariff_key=self.tariff.key,
        )
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
        static = static_plan(tomorrow, load, solar, static_params, tariff=self.tariff)
        if self.ups_active:
            return self._ups_preview(
                tomorrow,
                params,
                prices,
                load,
                static,
                static_params,
                today_greedy_end_kwh,
                padded=series.padded,
            )
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

    # Mirrors the shape of the inline greedy preview it replaces; the
    # arguments are all values _tomorrow_preview has already computed,
    # and recomputing them here would risk the two diverging.
    def _ups_preview(  # noqa: PLR0913
        self,
        tomorrow: date,
        params: BatteryParams,
        prices: list[float],
        load: list[float],
        static: Plan,
        static_params: BatteryParams,
        today_end_kwh: float | None,
        *,
        padded: bool,
    ) -> dict[str, Any]:
        """
        Tomorrow's UPS plan (ADR-0011), seeded from today's UPS end.

        A UPS day ends wherever it charged to, so today's planned end
        is the honest seed — the same chaining convention the greedy
        preview uses, applied to the plan that is actually running.
        It goes through the same clamp as every other UPS seed:
        today's plan ends a hair under capacity by design, and the
        arming headroom has to survive into tomorrow.
        """
        ups_params = dataclasses.replace(
            params,
            soc_start_kwh=ups_start_soc_kwh(
                today_end_kwh
                if today_end_kwh is not None
                else self.measured_start_soc_kwh(tomorrow),
                params,
            ),
        )
        plan = ups_plan(tomorrow, load, ups_params, prices, tariff=self.tariff)
        static_soc = [
            round(v, 3)
            for v in soc_trajectory(static, static_params, include_self_discharge=True)
        ]
        return {
            "tomorrow_prices_eur_kwh": [round(p, 5) for p in prices],
            "tomorrow_prices_padded": padded,
            "tomorrow_charge_w": list(plan.charge_w),
            "tomorrow_discharge_w": list(plan.discharge_w),
            "tomorrow_static_charge_w": list(static.charge_w),
            "tomorrow_static_discharge_w": list(static.discharge_w),
            "tomorrow_plan_soc_kwh": [
                round(v, 3)
                for v in soc_trajectory(plan, ups_params, include_self_discharge=True)
            ],
            "tomorrow_static_soc_kwh": static_soc,
        }

    async def _forecast_load_vector(self, today: date, n: int) -> list[float]:
        """
        Net load forecast (W) for advisory-plan input only (Task 11).

        Flat `BASE_LOAD_W` without a configured meter, or when history
        is too thin — `forecast_load` itself applies the <4-same-
        weekday-occurrences fallback per slot and for the whole day.

        Owner 2026-08-27 (forecast-accuracy hardening, Stage 2): the
        quarter-resolution load archive (`archive.async_recent_load_days`,
        accumulating since Task 11 decision 5) wins per date over
        `load_history.py`'s hour-expanded recorder adapter, which still
        covers whatever dates the archive hasn't reached yet
        (`merge_day_samples`) — this is decision 5's "supersedes the
        hourly adapter once enough days have accumulated", finally
        wired up.

        Stage 3: when `today` really is today (never for tomorrow's
        preview, which calls this too — see `_apply_intraday_correction`),
        the remaining hours are additionally scaled by how today has
        actually run so far.
        """
        merged = {**self.entry.data, **self.entry.options}
        entity_id = merged.get(CONF_LOAD_SENSOR)
        if not entity_id:
            return [BASE_LOAD_W] * n
        archived = await async_recent_load_days(self.hass, today, LOOKBACK_DAYS)
        adapter = await async_load_samples(self.hass, entity_id, today)
        samples = merge_day_samples(archived, adapter)
        solar = [0.0] * n
        forecast = forecast_load(today, samples, solar, n_intervals=n)
        if today == dt_util.now().date():
            forecast = await self._apply_intraday_correction(today, entity_id, forecast)
        return forecast

    async def _apply_intraday_correction(
        self,
        today: date,
        entity_id: str,
        forecast: list[float],
    ) -> list[float]:
        """
        Scale `forecast`'s remaining hours by today's own load so far (Stage 3).

        `async_load_samples`'s window always ends at start-of-day for
        the `day` argument, so querying for TOMORROW with a 1-day
        lookback covers exactly [start of today, start of tomorrow) —
        today's own hours. The recorder only materialises an hourly
        long-term-statistics row once that hour CLOSES, so any hour
        not yet finished (and the rest of the day) comes back absent —
        exactly `apply_intraday_correction`'s "not yet observed"
        convention, with no extra bookkeeping needed for "now".

        A length mismatch (a DST-shifted 92/100-interval day the
        hour-expanded adapter can't align to `forecast`'s own vector —
        not material, CONTEXT.md) skips correction rather than
        misaligning quarters.
        """
        tomorrow = today + timedelta(days=1)
        todays_samples = await async_load_samples(
            self.hass, entity_id, tomorrow, lookback_days=1
        )
        observed = next((s for s in todays_samples if s.day == today), None)
        if observed is None or len(observed.load_w) != len(forecast):
            return forecast
        return apply_intraday_correction(forecast, observed.load_w)

    async def _prices_for_day(self, day: date) -> DaySeries | None:
        """
        Price series for a Lisbon-local day under the entry's tariff.

        `omie.get_prices_for_date` needs market dates D and D+1 to
        cover a Lisbon day; a missing D+1 pads the final hour,
        flagged on the returned series. Shared by today's plan
        (decisions 4/6) and tomorrow's preview (decision 9).

        ADR-0012: a fixa contract never calls OMIE — its day is always
        priceable. Indexada Média estimates the window average from
        the archived OMIE of the trailing `MEDIA_LOOKBACK_DAYS` plus
        the day itself.
        """
        if not self.tariff.needs_omie:
            return fixed_day_series(day, self.tariff)
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
        history: list[tuple[datetime, float]] = []
        if self.tariff.pricing == "indexada_media":
            history = await async_recent_omie_points(
                self.hass, day, MEDIA_LOOKBACK_DAYS
            )
        return day_series_from_service(day, entries, self.tariff, history)

    def _advisory_plan(
        self,
        params: BatteryParams,
        prices: list[float] | None,
        load: list[float],
    ) -> dict[str, Any]:
        """Compute today's capped-greedy plan, or the static fallback."""
        today = dt_util.now().date()
        solar = [0.0] * len(load)
        if self.ups_active:
            return self._ups_plan_data(today, params, prices, load, solar)
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
            return self._static_fallback(today, load, solar, static_params, self.tariff)
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
            return self._static_fallback(today, load, solar, static_params, self.tariff)
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
        static = static_plan(today, load, solar, static_params, tariff=self.tariff)
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

    def _ups_plan_data(
        self,
        today: date,
        params: BatteryParams,
        prices: list[float] | None,
        load: list[float],
        solar: list[float],
    ) -> dict[str, Any]:
        """
        UPS mode (ADR-0011): charge on the cheapest energy, never discharge.

        The static baseline is still published alongside — that is the
        arbitrage the owner is giving up, and `vs_static_eur` prices
        it. With no delivered prices the plan is still built (TAR
        ranks the intervals offline), and only the money figures go
        unreported.

        The greedy chain record is deliberately NOT written here: it
        tracks the arbitrage battery's own trajectory, and a UPS day
        has nothing to say about where that battery would have been.
        Toggling the mode off therefore resumes the greedy chain from
        its last real day, and ADR-0010's measured seed corrects it on
        the first midnight after.
        """
        ups_params = dataclasses.replace(
            params,
            soc_start_kwh=ups_start_soc_kwh(self.measured_start_soc_kwh(today), params),
        )
        plan = ups_plan(today, load, ups_params, prices, tariff=self.tariff)
        static_params = dataclasses.replace(
            params, soc_start_kwh=self._day_start_soc(today, load, solar, params)
        )
        if validate_plan(plan, load, solar, ups_params):
            # Unreachable by construction (the builder emits no
            # discharge and stops at the ceiling), so this is
            # belt-and-braces. Publishing the static baseline here
            # cannot leak a discharge into the battery: the executor
            # branches on the live UPS flag, not on what is published,
            # and with nothing published it builds its own UPS plan.
            _LOGGER.warning("UPS plan failed validation; publishing the baseline")
            self.executor_plan = None
            return {
                **self._static_fallback(today, load, solar, static_params, self.tariff),
                "ups_mode": True,
            }
        self.executor_plan = DynamicDayPlan(
            day=today,
            plan=plan,
            params=ups_params,
            load_w=tuple(load),
            solar_w=tuple(solar),
            ups=True,
        )
        static = static_plan(today, load, solar, static_params, tariff=self.tariff)
        ups_soc = soc_trajectory(plan, ups_params, include_self_discharge=True)
        # A UPS plan only buys, so its "saving" is negative: it is the
        # standing cost of holding the reserve. Reported as-is — the
        # owner asked for backup, not for a flattering number.
        ups_saving = (
            saving_vs_no_cycling(plan, prices, ups_params)
            if prices is not None
            else None
        )
        static_saving = (
            saving_vs_no_cycling(static, prices, static_params)
            if prices is not None
            else None
        )
        return {
            "prices_ok": prices is not None,
            "ups_mode": True,
            "plan_date": today,
            "plan_charge_w": list(plan.charge_w),
            "plan_discharge_w": list(plan.discharge_w),
            "plan_soc_kwh": [round(v, 3) for v in ups_soc],
            "static_charge_w": list(static.charge_w),
            "static_discharge_w": list(static.discharge_w),
            "static_soc_kwh": [
                round(v, 3)
                for v in soc_trajectory(
                    static, static_params, include_self_discharge=True
                )
            ],
            "forecast_saving_eur": (
                None if ups_saving is None else round(ups_saving, 4)
            ),
            "vs_static_eur": (
                None
                if ups_saving is None or static_saving is None
                else round(ups_saving - static_saving, 4)
            ),
            # Not a fallback: this IS the requested plan. The TAR
            # ranking used when prices are missing is flagged by
            # `prices_ok`, exactly as everywhere else.
            "fallback": None,
        }

    @staticmethod
    def _static_fallback(
        today: date,
        load: list[float],
        solar: list[float],
        plan_params: BatteryParams,
        tariff: Tariff,
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
        fallback_plan = static_plan(today, load, solar, plan_params, tariff=tariff)
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
        # Same merge as _forecast_load_vector (owner 2026-08-27): the
        # MAE must measure the forecast method actually driving the
        # plan, not a stale adapter-only one. `yesterday` itself is
        # excluded by construction — async_recent_load_days only reads
        # strictly-earlier archive files — so this stays "history
        # available before yesterday" as documented above.
        archived = await async_recent_load_days(self.hass, yesterday, LOOKBACK_DAYS)
        adapter = await async_load_samples(
            self.hass, entity_id, yesterday, lookback_days=LOOKBACK_DAYS
        )
        forecast_samples = merge_day_samples(archived, adapter)
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
