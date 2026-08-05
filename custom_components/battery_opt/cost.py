"""
Cost-today tracker (plan Task 13 pulled forward, decision 8).

`CostToday` is the pure accumulator (variable + fixed EUR for one
calendar day) — trivially unit-testable, no homeassistant imports.
`CostTracker` is the thin HA-side wiring: it listens to
`CONF_GRID_ENERGY_SENSOR` state changes, prices each delta at the
delivered price for that instant, and persists across restarts via
its own `homeassistant.helpers.storage.Store` (kept separate from the
load-MAE Store in coordinator.py — decision 8 says "the same Store",
read here as "the same storage MECHANISM": a shared file would need
read-merge-write on every write from two independently-triggered
writers — state-change events here vs. the 00:05 day close there —
which is exactly the kind of race a single Store per concern avoids).

VAT is deliberately excluded: the reduced rate on the first 200
kWh/30 days makes it a billing-window computation, not a per-quarter
one — revisit alongside Task 13's invoice reconciliation, which needs
that logic anyway (docs/plan.md's Phase 3 note).

`fixed_eur` (K3 + TAR_POTENCIA_2026, EUR/day) is a uniform constant
that never changes the optimum (spec §4) — it is added once per day,
here, for reporting only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .core.prices import K3, TAR_POTENCIA_2026

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import Event, EventStateChangedData, HomeAssistant

FIXED_EUR_PER_DAY = K3 + TAR_POTENCIA_2026
_STORE_VERSION = 1
_BAD_STATES = frozenset({"unavailable", "unknown", "none", ""})


@dataclass
class CostToday:
    """One calendar day's accumulated grid-import cost, EUR, excl. VAT."""

    day: str  # ISO date; a plain str so the Store payload round-trips as-is
    variable_eur: float = 0.0
    energy_today_kwh: float = 0.0
    fixed_eur: float = FIXED_EUR_PER_DAY

    def add_delta(self, delta_kwh: float, price_eur_kwh: float | None) -> None:
        """
        Apply one grid-import energy delta.

        Callers must floor a negative delta (meter reset) to zero
        before calling — decision 8: "a negative delta counts as 0".
        A delta with no known price (prices_ok False at that instant)
        still counts as energy consumed, just not as cost — rare: only
        before the first successful daily price fetch.
        """
        if delta_kwh <= 0:
            return
        self.energy_today_kwh += delta_kwh
        if price_eur_kwh is not None:
            self.variable_eur += delta_kwh * price_eur_kwh

    @property
    def total_eur(self) -> float:
        """Variable + fixed, excl. VAT (spec §4 / decision 8)."""
        return self.variable_eur + self.fixed_eur

    def as_dict(self) -> dict[str, Any]:
        """Store-round-trippable payload."""
        return {
            "day": self.day,
            "variable_eur": self.variable_eur,
            "energy_today_kwh": self.energy_today_kwh,
            "fixed_eur": self.fixed_eur,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CostToday:
        """Rebuild from a Store payload; missing keys take today's defaults."""
        return cls(
            day=payload["day"],
            variable_eur=payload.get("variable_eur", 0.0),
            energy_today_kwh=payload.get("energy_today_kwh", 0.0),
            fixed_eur=payload.get("fixed_eur", FIXED_EUR_PER_DAY),
        )


def _state_value(raw: str) -> float | None:
    if raw.lower() in _BAD_STATES:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class CostTracker:
    """
    Wires `CostToday` to a grid-import energy sensor's state changes.

    Self-contained: registers both the state-change listener and its
    own local-midnight reset trigger, so it needs no cooperation from
    `__init__.py`'s existing day-close job (that job is Task 11's
    load-MAE concern only).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        entity_id: str,
        get_price_eur_kwh: Callable[[], float | None],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Bind to the meter entity and the coordinator's price lookup."""
        self.hass = hass
        self.entity_id = entity_id
        self._get_price_eur_kwh = get_price_eur_kwh
        self._on_change = on_change or (lambda: None)
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORE_VERSION, f"{DOMAIN}_{entry_id}_cost_today"
        )
        self.state = CostToday(day=dt_util.now().date().isoformat())
        self._last_energy_kwh: float | None = None
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_midnight: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Restore today's accumulation (if any) and start tracking."""
        stored = await self._store.async_load()
        today = dt_util.now().date().isoformat()
        self.state = (
            CostToday.from_dict(stored)
            if stored is not None and stored.get("day") == today
            else CostToday(day=today)
        )
        current = self.hass.states.get(self.entity_id)
        if current is not None:
            self._last_energy_kwh = _state_value(current.state)
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.entity_id], self._handle_state_change
        )
        self._unsub_midnight = async_track_time_change(
            self.hass, self._handle_midnight, hour=0, minute=0, second=0
        )

    def async_stop(self) -> None:
        """Unsubscribe both listeners."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None

    def _maybe_roll_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if self.state.day != today:
            self.state = CostToday(day=today)
            self._last_energy_kwh = None  # a stale cross-day delta is not consumption

    @callback
    def _handle_midnight(self, _now: Any) -> None:
        self._maybe_roll_day()
        self.hass.async_create_task(self._store.async_save(self.state.as_dict()))
        self._on_change()

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._maybe_roll_day()
        new_state = event.data["new_state"]
        if new_state is None:
            return
        new_energy = _state_value(new_state.state)
        if new_energy is None:
            return
        if self._last_energy_kwh is not None:
            delta = max(0.0, new_energy - self._last_energy_kwh)
            if delta > 0:
                self.state.add_delta(delta, self._get_price_eur_kwh())
                self.hass.async_create_task(
                    self._store.async_save(self.state.as_dict())
                )
                self._on_change()
        self._last_energy_kwh = new_energy
