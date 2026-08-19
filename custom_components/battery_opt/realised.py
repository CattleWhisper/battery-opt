"""
Realised-savings tracker (plan Task 13): HA wiring for core.reporting.

Mirrors cost.py's tracker pattern. `RealisedTracker` listens to the
battery power sensors (the same entities the ADR-0007 charge loop
reads, following the HA battery convention: positive W = DIScharging,
owner 2026-08-11 — one per fleet unit, ADR-0009), integrates the held
SUM over its elapsed interval at the delivered price of that instant,
and persists day + month across restarts via its own Store. The sign
is negated at the core boundary — `core.reporting` books
charge-positive. An interval where ANY sensor is unavailable
contributes nothing: a partial fleet sum would misbook a unit's flow.

At local midnight the closed day folds into the month ledger; when
the month changes (or a restart lands in a new month), the monthly
reconciliation report goes out as a persistent notification —
deviation beyond ±10% is flagged in the body (spec Task 13). The
coordinator listener keeps each day's forecast saving recorded in the
ledger, so the comparison needs no end-of-day capture race.

A gap between power samples longer than MAX_SAMPLE_GAP_S contributes
nothing: holding a stale power reading over a long outage would
fabricate energy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.reporting import MonthLedger, RealisedDay

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import Event, EventStateChangedData, HomeAssistant

    from .coordinator import BatteryOptCoordinator

_STORE_VERSION = 1
_BAD_STATES = frozenset({"unavailable", "unknown", "none", ""})

# Longer sample gaps integrate as zero — the sensor was gone, not flat.
MAX_SAMPLE_GAP_S = 900.0


def _state_value(raw: str) -> float | None:
    if raw.lower() in _BAD_STATES:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class RealisedTracker:
    """Wires RealisedDay/MonthLedger to the battery power sensor."""

    def __init__(
        self,
        coordinator: BatteryOptCoordinator,
        entry_id: str,
        entity_ids: list[str],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Bind to the power entities and the coordinator's price lookup."""
        self.hass: HomeAssistant = coordinator.hass
        self._coordinator = coordinator
        self.entity_ids = entity_ids
        self._on_change = on_change or (lambda: None)
        self._store: Store[dict[str, Any]] = Store(
            self.hass, _STORE_VERSION, f"{DOMAIN}_{entry_id}_realised"
        )
        now = dt_util.now()
        self.state = RealisedDay(
            day=now.date().isoformat(),
            wear_cost_eur_kwh=coordinator.battery_params.wear_cost_eur_kwh,
        )
        self.ledger = MonthLedger(month=now.date().isoformat()[:7])
        self._last_power_w: dict[str, float | None] = dict.fromkeys(entity_ids)
        self._last_sample_at: datetime | None = None
        self._unsubs: list[Callable[[], None]] = []

    def _total_power_w(self) -> float | None:
        """Held fleet total; None while ANY sensor's value is unknown."""
        total = 0.0
        for value in self._last_power_w.values():
            if value is None:
                return None
            total += value
        return total

    async def async_start(self) -> None:
        """Restore persisted state, then start all listeners."""
        stored = await self._store.async_load()
        if stored is not None:
            if "ledger" in stored:
                self.ledger = MonthLedger.from_dict(stored["ledger"])
            if stored.get("day", {}).get("day"):
                restored = RealisedDay.from_dict(stored["day"])
                if restored.day == self.state.day:
                    self.state = restored
                else:
                    # Restart across midnight: close the stored day now.
                    self.ledger.fold_day(restored)
        self._maybe_roll_month()
        for entity_id in self.entity_ids:
            current = self.hass.states.get(entity_id)
            if current is not None:
                self._last_power_w[entity_id] = _state_value(current.state)
                self._last_sample_at = dt_util.utcnow()
        self._unsubs = [
            async_track_state_change_event(
                self.hass, self.entity_ids, self._handle_state_change
            ),
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=0
            ),
            self._coordinator.async_add_listener(self._handle_coordinator_update),
        ]

    def async_stop(self) -> None:
        """Unsubscribe every listener."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    def _save(self) -> None:
        self.hass.async_create_task(
            self._store.async_save(
                {"day": self.state.as_dict(), "ledger": self.ledger.as_dict()}
            )
        )

    def _maybe_roll_month(self) -> None:
        month = dt_util.now().date().isoformat()[:7]
        if self.ledger.month != month:
            self._notify_report(self.ledger)
            self.ledger = MonthLedger(month=month)

    def _notify_report(self, ledger: MonthLedger) -> None:
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": f"Battery Opt: monthly report {ledger.month}",
                    "message": ledger.report(),
                },
            )
        )

    def _maybe_roll_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if self.state.day != today:
            self.ledger.fold_day(self.state)
            self._maybe_roll_month()
            self.state = RealisedDay(
                day=today,
                wear_cost_eur_kwh=(self._coordinator.battery_params.wear_cost_eur_kwh),
            )
            # A power sample held across midnight stays valid: the
            # integration below simply books the post-midnight slice
            # of it into the new day.

    @callback
    def _handle_midnight(self, _now: Any) -> None:
        self._integrate(dt_util.utcnow())
        self._maybe_roll_day()
        self._save()
        self._on_change()

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self._coordinator.data or {}
        plan_date = data.get("plan_date")
        forecast = data.get("forecast_saving_eur")
        if plan_date is None or forecast is None:
            return
        self.ledger.record_forecast(str(plan_date), forecast)
        self._save()

    def _integrate(self, now: datetime) -> None:
        """Book the held fleet total over the interval ending now."""
        total_w = self._total_power_w()
        if total_w is None or self._last_sample_at is None:
            self._last_sample_at = now
            return
        elapsed_s = (now - self._last_sample_at).total_seconds()
        self._last_sample_at = now
        if elapsed_s <= 0 or elapsed_s > MAX_SAMPLE_GAP_S:
            return
        # Sensors are HA-convention (positive = discharging); the core
        # books charge-positive — negate at the boundary.
        self.state.add_interval(
            -total_w,
            elapsed_s / 3600.0,
            self._coordinator.current_price_eur_kwh(),
        )

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._maybe_roll_day()
        new_state = event.data["new_state"]
        if new_state is None:
            return
        self._integrate(dt_util.utcnow())
        self._last_power_w[event.data["entity_id"]] = _state_value(new_state.state)
        self._save()
        self._on_change()
