"""
Sensors for battery_opt (spec §8).

- sensor.battery_opt_plan: with a battery, the executor's last
  commanded action; in planning-only mode, the advisory plan's action
  for the current quarter-hour. Either way the attributes carry
  `schedule`: the advisory plan as merged charge/discharge windows
  (ISO start/end, direction, power_w), spanning today and — once the
  D+1 preview builds — tomorrow in one flat list.
- sensor.battery_opt_forecast_savings: the advisory capped-greedy
  plan's forecast saving vs not cycling (EUR/day, excl. fixed terms
  and VAT), from real OMIE prices.
- sensor.battery_opt_vs_static: forecast gain vs the fixed seasonal
  schedule — the metric that justifies the project (spec §8).
- sensor.battery_opt_current_price: the delivered energy price right
  now per the EDP Indexada formula (core.prices.price), EUR/kWh excl.
  fixed terms and VAT. Declared exactly like core OMIE's price sensor
  so the Energy dashboard accepts it as a grid price entity.
- sensor.battery_opt_best_periods: start of the next best period to
  run high-power appliances (timestamp), with both days' maximal
  cheap windows in the attributes — the dashboard face of the
  `battery_opt.get_best_periods` service, at the same defaults.
- sensor.battery_opt_load_mae: mean absolute error (W) of yesterday's
  load forecast vs the observed load, computed at day close (plan
  Task 11, decision 7). Unknown until a load meter is configured and
  one full day has closed.
- sensor.battery_opt_cost_today: grid-import cost today (EUR, excl.
  VAT), variable (meter deltas x delivered price) plus the daily
  fixed terms (plan Task 13 pulled forward, decision 8). Unavailable
  without CONF_GRID_ENERGY_SENSOR configured.
- sensor.battery_opt_realised_savings: today's realised saving from
  MEASURED battery flows (plan Task 13) — discharge value minus
  charge cost minus true wear, integrated from the battery power
  sensor; month-to-date realised/forecast and their deviation in the
  attributes. Unavailable without CONF_BATTERY_POWER_SENSOR.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    BEST_PERIODS_COUNT,
    BEST_PERIODS_MIN_QUARTERS,
    BEST_PERIODS_THRESHOLD_PCT,
    CONF_BATTERY_POWER_SENSOR,
    CONF_GRID_ENERGY_SENSOR,
)
from .core.appliance import cheap_periods, expensive_periods, price_cutoff
from .core.calendar import period
from .core.plan import price_segments, schedule_segments
from .cost import CostTracker
from .entity import device_info_for
from .realised import RealisedTracker

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import BatteryOptConfigEntry
    from .charge_loop import ChargePowerLoop
    from .coordinator import BatteryOptCoordinator
    from .executor import BatteryOptExecutor


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - HA-required signature
    entry: BatteryOptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the plan, savings, price, load-MAE and cost sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            PlanSensor(
                runtime.coordinator,
                runtime.executor,
                entry.entry_id,
                charge_loop=runtime.charge_loop,
            ),
            ForecastSavingsSensor(runtime.coordinator, entry.entry_id),
            VsStaticSensor(runtime.coordinator, entry.entry_id),
            CurrentPriceSensor(runtime.coordinator, entry.entry_id),
            BestPeriodsSensor(runtime.coordinator, entry.entry_id),
            SocForecastSensor(runtime.coordinator, runtime.executor, entry.entry_id),
            LoadMaeSensor(runtime.coordinator, entry.entry_id),
            CostTodaySensor(runtime.coordinator, entry.entry_id),
            RealisedSavingsSensor(runtime.coordinator, entry.entry_id),
        ]
    )


class QuarterHourMixin(SensorEntity):
    """
    Rewrite state on quarter-hour boundaries.

    The coordinator refresh is not wall-clock aligned, but prices and
    plan slots change exactly at :00/:15/:30/:45 — without this the
    state lags the boundary by up to a full update interval.
    """

    async def async_added_to_hass(self) -> None:
        """Register the quarter-hour rewrite."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._on_quarter_hour, minute=[0, 15, 30, 45], second=0
            )
        )

    @callback
    def _on_quarter_hour(self, _now: datetime) -> None:
        self.async_write_ha_state()


class PlanSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """Current action plus the advisory day plan."""

    _attr_has_entity_name = True
    _attr_name = "Plan"
    _attr_suggested_object_id = "battery_opt_plan"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor | None,
        entry_id: str,
        charge_loop: ChargePowerLoop | None = None,
    ) -> None:
        """Bind to the coordinator and, when present, the executor."""
        super().__init__(coordinator)
        self._executor = executor
        self._charge_loop = charge_loop
        self._attr_unique_id = f"{entry_id}_plan"
        self._attr_device_info = device_info_for(entry_id)

    async def async_added_to_hass(self) -> None:
        """Also refresh whenever the executor state changes."""
        await super().async_added_to_hass()
        if self._executor is not None:
            self.async_on_remove(
                self._executor.add_listener(self._handle_executor_change)
            )

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Executor's last command, or the advisory action right now."""
        if self._executor is not None:
            return self._executor.last_action
        return self.coordinator.planned_action_now()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The advisory plan, and the executor state when it exists."""
        data = self.coordinator.data or {}
        attributes: dict[str, Any] = {
            "mode": "active" if self._executor is not None else "planning_only",
            "plan_date": str(data.get("plan_date") or ""),
            "schedule": self._segments(
                data,
                "plan_charge_w",
                "plan_discharge_w",
                tomorrow_keys=("tomorrow_charge_w", "tomorrow_discharge_w"),
            ),
            # The static baseline as the same segment format — graph it
            # against `schedule` to compare the two plans (the
            # Checkpoint C / Task 12 dry-run comparison view).
            "static_schedule": self._segments(
                data,
                "static_charge_w",
                "static_discharge_w",
                tomorrow_keys=(
                    "tomorrow_static_charge_w",
                    "tomorrow_static_discharge_w",
                ),
            ),
            "prices_ok": data.get("prices_ok"),
            "prices_padded": data.get("prices_padded"),
            # Decision 6: set to "static" whenever no trustworthy
            # dynamic plan exists (missing prices, or defensively an
            # invalid solve) and the fixed seasonal schedule is
            # published instead; None otherwise.
            "fallback": data.get("fallback"),
        }
        if self._executor is not None:
            attributes["executor_status"] = self._executor.status
            attributes["executor_plan_date"] = str(self._executor.plan_day or "")
            # Task 12: what the executor is actuating — "static"
            # (dry-run), "greedy" (dynamic live) or "static-fallback"
            # (dynamic enabled, no trustworthy greedy for today).
            attributes["executor_plan_source"] = self._executor.plan_source
        if self._charge_loop is not None:
            # ADR-0007 / Task 15 criterion 5: fallback is flagged here.
            attributes["charge_loop_setpoint_w"] = self._charge_loop.last_setpoint_w
            attributes["charge_loop_fallback"] = self._charge_loop.fallback
        return attributes

    @staticmethod
    def _segments(
        data: dict[str, Any],
        charge_key: str,
        discharge_key: str,
        tomorrow_keys: tuple[str, str],
    ) -> list[dict[str, Any]]:
        """
        Merge a plan's vectors into multi-day charge/discharge windows.

        Today's segments, extended with tomorrow's the moment the D+1
        preview builds (decision 9) — the ISO timestamps carry the
        date, so one flat list spans both days. Shared by the advisory
        `schedule` and the `static_schedule` baseline.
        """
        plan_date = data.get("plan_date")
        if plan_date is None:
            return []
        segments: list[dict[str, Any]] = []
        charge = data.get(charge_key)
        discharge = data.get(discharge_key)
        if charge and discharge:
            segments += schedule_segments(plan_date, charge, discharge)
        tomorrow_charge = data.get(tomorrow_keys[0])
        tomorrow_discharge = data.get(tomorrow_keys[1])
        if tomorrow_charge and tomorrow_discharge:
            segments += schedule_segments(
                plan_date + timedelta(days=1), tomorrow_charge, tomorrow_discharge
            )
        return segments


class ForecastSavingsSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Advisory plan's forecast saving vs not cycling, EUR/day."""

    _attr_has_entity_name = True
    _attr_name = "Forecast savings"
    _attr_suggested_object_id = "battery_opt_forecast_savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_forecast_savings"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """Excl. fixed terms and VAT (spec §4); None until prices exist."""
        return (self.coordinator.data or {}).get("forecast_saving_eur")


class CurrentPriceSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """Delivered energy price now, per the EDP Indexada formula."""

    _attr_has_entity_name = True
    _attr_name = "Current price"
    _attr_suggested_object_id = "battery_opt_current_price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}"
    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_current_price"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """EUR/kWh excl. fixed terms and VAT; None until prices exist."""
        return self.coordinator.current_price_eur_kwh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the TAR period and the day's prices for graphing."""
        data = self.coordinator.data or {}
        return {
            "tar_period": period(dt_util.now()),
            "plan_date": str(data.get("plan_date") or ""),
            "prices": self._price_segments(data),
            "prices_padded": data.get("prices_padded"),
            # Decision 9: tomorrow joins `prices` only once D+1 itself
            # builds. Its padding flag stays separate — structurally
            # almost always "yes" (D+2 never exists yet), exposed for
            # symmetry with prices_padded and honesty about the last
            # hour. None while there is no preview at all.
            "tomorrow_prices_padded": data.get("tomorrow_prices_padded"),
        }

    @staticmethod
    def _price_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Merge the delivered prices into multi-day display segments.

        Today's prices, extended with tomorrow's the moment the D+1
        preview builds — one flat list, each segment carrying its TAR
        period so the value is checkable against the tariff table.
        """
        plan_date = data.get("plan_date")
        if plan_date is None:
            return []
        segments: list[dict[str, Any]] = []
        prices = data.get("prices_eur_kwh")
        if prices:
            segments += price_segments(plan_date, prices)
        tomorrow_prices = data.get("tomorrow_prices_eur_kwh")
        if tomorrow_prices:
            segments += price_segments(plan_date + timedelta(days=1), tomorrow_prices)
        return segments


class BestPeriodsSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """
    Start of the next best period to run high-power appliances.

    The dashboard face of the `get_best_periods` service, computed
    with the same core semantics at the shared defaults: periods are
    MAXIMAL contiguous cheap runs (at or below min + 20% of the day's
    price range, at least 30 min long, top 3, in time order). The
    state is the start of the next period that has not ended yet
    (today's, else tomorrow's); the attributes carry both days' lists
    — same shape as the service response, so one graph card covers
    the 48 h view — plus each day's cheap cutoff for a threshold line,
    and the mirrored EXPENSIVE tier (maximal runs at the top of the
    range) for the traffic-light day strip: cheap green, expensive
    red, the complement in between.
    """

    _attr_has_entity_name = True
    _attr_name = "Best periods"
    _attr_suggested_object_id = "battery_opt_best_periods"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:washing-machine"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_best_periods"
        self._attr_device_info = device_info_for(entry_id)

    def _day_prices(self, offset_days: int) -> list[float] | None:
        data = self.coordinator.data or {}
        return data.get(
            "prices_eur_kwh" if offset_days == 0 else "tomorrow_prices_eur_kwh"
        )

    def _build_periods(
        self,
        offset_days: int,
        build: Callable[..., list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Run a tier builder for plan_date + offset; [] without prices."""
        data = self.coordinator.data or {}
        plan_date = data.get("plan_date")
        prices = self._day_prices(offset_days)
        if plan_date is None or not prices:
            return []
        return build(
            plan_date + timedelta(days=offset_days),
            prices,
            BEST_PERIODS_THRESHOLD_PCT / 100.0,
            BEST_PERIODS_MIN_QUARTERS,
            BEST_PERIODS_COUNT,
        )

    def _day_periods(self, offset_days: int) -> list[dict[str, Any]]:
        """Cheap periods for plan_date + offset; [] without prices."""
        return self._build_periods(offset_days, cheap_periods)

    def _day_cutoff(self, offset_days: int) -> float | None:
        prices = self._day_prices(offset_days)
        if not prices:
            return None
        cutoff = price_cutoff(prices, BEST_PERIODS_THRESHOLD_PCT / 100.0)
        return None if cutoff is None else round(cutoff, 5)

    @staticmethod
    def _day_avg(prices: list[float] | None) -> float | None:
        return round(sum(prices) / len(prices), 5) if prices else None

    @property
    def native_value(self) -> datetime | None:
        """Start of the next (or current) best period across both days."""
        now = dt_util.now()
        upcoming = [
            datetime.fromisoformat(str(p["start"]))
            for p in self._day_periods(0) + self._day_periods(1)
            if datetime.fromisoformat(str(p["end"])) > now
        ]
        return min(upcoming, default=None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Both days' cheap periods (time order) for graphing."""
        data = self.coordinator.data or {}
        return {
            "plan_date": str(data.get("plan_date") or ""),
            "threshold_pct": BEST_PERIODS_THRESHOLD_PCT,
            "min_duration_minutes": 15 * BEST_PERIODS_MIN_QUARTERS,
            "count": BEST_PERIODS_COUNT,
            "periods": self._day_periods(0),
            "tomorrow_periods": self._day_periods(1),
            # The mirrored top-of-range tier — "avoid these" — for the
            # traffic-light day strip (README): green cheap, red
            # expensive, yellow the complement (computed card-side).
            "expensive_periods": self._build_periods(0, expensive_periods),
            "tomorrow_expensive_periods": self._build_periods(1, expensive_periods),
            "threshold_price_eur_kwh": self._day_cutoff(0),
            "tomorrow_threshold_price_eur_kwh": self._day_cutoff(1),
            "day_avg_price_eur_kwh": self._day_avg(data.get("prices_eur_kwh")),
            "tomorrow_day_avg_price_eur_kwh": self._day_avg(
                data.get("tomorrow_prices_eur_kwh")
            ),
        }


class SocForecastSensor(QuarterHourMixin, CoordinatorEntity["BatteryOptCoordinator"]):
    """
    Planned SoC for the current quarter-hour, in percent.

    Same unit as the Marstek SoC sensor so the two plot directly
    against each other — the forecast-vs-real comparison Checkpoint C
    watches. Source is the executor's actual plan when a battery
    actuates, the advisory plan otherwise; the full day trajectory
    (97 boundary values, index i = start of quarter i) rides in the
    attributes in both kWh and percent.
    """

    _attr_has_entity_name = True
    _attr_name = "SoC forecast"
    _attr_suggested_object_id = "battery_opt_soc_forecast"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:battery-clock"

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        executor: BatteryOptExecutor | None,
        entry_id: str,
    ) -> None:
        """Bind to the coordinator and, when present, the executor."""
        super().__init__(coordinator)
        self._executor = executor
        self._attr_unique_id = f"{entry_id}_soc_forecast"
        self._attr_device_info = device_info_for(entry_id)

    async def async_added_to_hass(self) -> None:
        """Also refresh whenever the executor state changes."""
        await super().async_added_to_hass()
        if self._executor is not None:
            self.async_on_remove(
                self._executor.add_listener(self._handle_executor_change)
            )

    def _handle_executor_change(self) -> None:
        self.async_write_ha_state()

    def _executor_trajectory(self) -> list[float] | None:
        """Return the actuated plan's trajectory when it is today's."""
        if self._executor is None or self._executor.plan_day != dt_util.now().date():
            return None
        return self._executor.planned_soc_trajectory()

    def _to_pct(self, kwh: float) -> float:
        cap = self.coordinator.battery_params.cap_usable_kwh
        return round(kwh / cap * 100.0, 1)

    @property
    def native_value(self) -> float | None:
        """Planned SoC (%) at the end of the current quarter-hour."""
        trajectory = self._executor_trajectory()
        if trajectory is not None:
            now = dt_util.now()
            index = min((now.hour * 60 + now.minute) // 15 + 1, len(trajectory) - 1)
            return self._to_pct(trajectory[index])
        kwh = self.coordinator.forecast_soc_kwh()
        return None if kwh is None else self._to_pct(kwh)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The day trajectory and where it came from."""
        data = self.coordinator.data or {}
        trajectory = self._executor_trajectory()
        if trajectory is not None:
            source = "executor"
            plan_date = str(self._executor.plan_day or "")
        else:
            trajectory = data.get("plan_soc_kwh")
            source = "advisory" if trajectory else None
            plan_date = str(data.get("plan_date") or "")
        # Both plans' trajectories for the comparison overlay (the
        # Task 12 dry-run view): in the static fallback there is no
        # greedy, and plan_soc_kwh already IS the static trajectory.
        # Once tomorrow's preview builds, both extend to the full 48 h
        # (193 boundary values; the duplicated midnight boundary is
        # dropped). Both lines are continuous across midnight: the
        # static chains its own end, and tomorrow's greedy is seeded
        # from TODAY'S greedy end (owner 2026-08-13) — never a per-day
        # counterfactual that resets at the static chain's seed.
        greedy_kwh = data.get("plan_soc_kwh") if data.get("fallback") is None else None
        tomorrow_greedy = data.get("tomorrow_plan_soc_kwh")
        if greedy_kwh and tomorrow_greedy:
            greedy_kwh = [*greedy_kwh, *tomorrow_greedy[1:]]
        static_kwh = data.get("static_soc_kwh")
        tomorrow_static = data.get("tomorrow_static_soc_kwh")
        if static_kwh and tomorrow_static:
            static_kwh = [*static_kwh, *tomorrow_static[1:]]
        return {
            "source": source,
            "plan_date": plan_date,
            "trajectory_kwh": (
                [round(v, 3) for v in trajectory] if trajectory else None
            ),
            "trajectory_pct": (
                [self._to_pct(v) for v in trajectory] if trajectory else None
            ),
            "greedy_trajectory_pct": (
                [self._to_pct(v) for v in greedy_kwh] if greedy_kwh else None
            ),
            "static_trajectory_pct": (
                [self._to_pct(v) for v in static_kwh] if static_kwh else None
            ),
        }


class VsStaticSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Forecast gain of the capped greedy over the static schedule."""

    _attr_has_entity_name = True
    _attr_name = "Vs static"
    _attr_suggested_object_id = "battery_opt_vs_static"
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_vs_static"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """EUR/day, excl. fixed terms and VAT; None until prices exist."""
        return (self.coordinator.data or {}).get("vs_static_eur")


class LoadMaeSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """Mean absolute error of yesterday's load forecast (plan Task 11)."""

    _attr_has_entity_name = True
    # "Load MAE" (not "Load forecast MAE"): the object id is derived
    # from this name's slug, and the decision-mandated entity_id is
    # sensor.battery_opt_load_mae.
    _attr_name = "Load MAE"
    _attr_suggested_object_id = "battery_opt_load_mae"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Bind to the coordinator."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_load_mae"
        self._attr_device_info = device_info_for(entry_id)

    @property
    def native_value(self) -> float | None:
        """
        W; unknown until a load meter is configured and one day closed.

        Computed at 00:05 local against the forecast that would have
        been made for yesterday using only history available before
        yesterday (decisions 5/7) — not a same-day self-comparison.
        """
        value = (self.coordinator.data or {}).get("load_mae_w")
        return round(value, 1) if value is not None else None


class CostTodaySensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """
    Grid-import cost today (plan Task 13 pulled forward, decision 8).

    `state_class` TOTAL with `last_reset` at local midnight: variable
    = Sigma(grid-import energy delta x delivered price at that
    instant, tracked from CONF_GRID_ENERGY_SENSOR state changes; a
    negative delta from a meter reset counts as 0), plus the fixed
    K3 + TAR_POTENCIA_2026 EUR/day term. VAT is excluded — the reduced
    rate on the first 200 kWh/30 days makes it a billing-window
    computation, not a per-quarter one; revisit alongside Task 13's
    invoice reconciliation. Unavailable without a configured meter.
    """

    _attr_has_entity_name = True
    _attr_name = "Cost today"
    _attr_suggested_object_id = "battery_opt_cost_today"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Build the tracker (if a meter is configured) and bind entity identity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_cost_today"
        self._attr_device_info = device_info_for(entry_id)
        merged = {**coordinator.entry.data, **coordinator.entry.options}
        entity_id = merged.get(CONF_GRID_ENERGY_SENSOR)
        self._tracker: CostTracker | None = (
            CostTracker(
                coordinator.hass,
                entry_id,
                entity_id,
                coordinator.current_price_eur_kwh,
                on_change=self.async_write_ha_state,
            )
            if entity_id
            else None
        )

    async def async_added_to_hass(self) -> None:
        """Start the tracker, if a meter is configured."""
        await super().async_added_to_hass()
        if self._tracker is not None:
            await self._tracker.async_start()
            self.async_on_remove(self._tracker.async_stop)

    @property
    def available(self) -> bool:
        """False without CONF_GRID_ENERGY_SENSOR configured (decision 1)."""
        return self._tracker is not None

    @property
    def native_value(self) -> float | None:
        """Variable + fixed, EUR, excl. VAT."""
        if self._tracker is None:
            return None
        return round(self._tracker.state.total_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Local midnight — the accumulation window start (decision 8)."""
        return dt_util.start_of_local_day(dt_util.now())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """variable_eur, fixed_eur, energy_today_kwh (decision 8)."""
        if self._tracker is None:
            return {}
        state = self._tracker.state
        return {
            "variable_eur": round(state.variable_eur, 4),
            "fixed_eur": round(state.fixed_eur, 4),
            "energy_today_kwh": round(state.energy_today_kwh, 4),
        }


class RealisedSavingsSensor(CoordinatorEntity["BatteryOptCoordinator"], SensorEntity):
    """
    Realised saving today from measured battery flows (plan Task 13).

    `state_class` TOTAL with `last_reset` at local midnight, like the
    cost sensor: Sigma(discharged kWh x delivered price) minus
    Sigma(charged kWh x delivered price) minus wear per discharged kWh
    (true wear — Checkpoint B books savings at true wear, plans at
    plan-wear). Flows integrate from CONF_BATTERY_POWER_SENSOR state
    changes (HA battery convention: positive W = discharging; the
    tracker negates into the core's charge-positive booking —
    ADR-0008: no SoC is read). Month-to-date totals and the
    realised-vs-forecast deviation ride in the attributes; the monthly
    reconciliation report is the tracker's persistent notification.
    Unavailable without a configured battery power sensor.
    """

    _attr_has_entity_name = True
    _attr_name = "Realised savings"
    _attr_suggested_object_id = "battery_opt_realised_savings"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash-check"

    def __init__(self, coordinator: BatteryOptCoordinator, entry_id: str) -> None:
        """Build the tracker (if a power sensor is configured)."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_realised_savings"
        self._attr_device_info = device_info_for(entry_id)
        merged = {**coordinator.entry.data, **coordinator.entry.options}
        entity_id = merged.get(CONF_BATTERY_POWER_SENSOR)
        self._tracker: RealisedTracker | None = (
            RealisedTracker(
                coordinator,
                entry_id,
                entity_id,
                on_change=self.async_write_ha_state,
            )
            if entity_id
            else None
        )

    async def async_added_to_hass(self) -> None:
        """Start the tracker, if a power sensor is configured."""
        await super().async_added_to_hass()
        if self._tracker is not None:
            await self._tracker.async_start()
            self.async_on_remove(self._tracker.async_stop)

    @property
    def available(self) -> bool:
        """False without CONF_BATTERY_POWER_SENSOR configured."""
        return self._tracker is not None

    @property
    def native_value(self) -> float | None:
        """Today's realised saving, EUR (excl. fixed terms and VAT)."""
        if self._tracker is None:
            return None
        return round(self._tracker.state.realised_eur, 4)

    @property
    def last_reset(self) -> datetime:
        """Local midnight — the accumulation window start."""
        return dt_util.start_of_local_day(dt_util.now())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Today's flows and the month-to-date reconciliation view."""
        if self._tracker is None:
            return {}
        state = self._tracker.state
        ledger = self._tracker.ledger
        deviation = ledger.deviation_pct()
        return {
            "charged_today_kwh": round(state.charged_kwh, 3),
            "discharged_today_kwh": round(state.discharged_kwh, 3),
            "month": ledger.month,
            "month_realised_eur": round(ledger.realised_eur, 4),
            "month_forecast_eur": round(ledger.forecast_eur, 4),
            "month_deviation_pct": (None if deviation is None else round(deviation, 1)),
        }
