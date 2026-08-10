"""
Realised savings and monthly reconciliation (plan Task 13).

Pure module: no I/O, no clock reads, no Home Assistant imports
(ADR-0001). `RealisedDay` integrates measured battery power into one
day's realised saving; `MonthLedger` folds days into a monthly
forecast-vs-realised comparison and renders the report the monthly
notification carries.

The plan's original wording ("from actual SoC and prices") predates
ADR-0008 — no SoC is read anywhere. The measured battery POWER sensor
replaces it: the same entity the ADR-0007 charge loop uses, with the
same sign convention (positive = charging, negative = discharging).
A discharged kWh displaces a grid import at that instant's delivered
price; a charged kWh is an extra import at that price; wear is booked
per kWh discharged at the TRUE wear cost (Checkpoint B: plans cap
cycling at the plan-wear, savings are always booked at true wear).

The invoice itself can never be an input — reconciling the first
month against the real invoice is Task 13's manual verification step.
The automated deviation is realised-vs-forecast; the monthly cost to
check against the invoice is the cost sensor's own monthly statistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Spec Task 13: a monthly deviation beyond this is flagged loudly in
# the report notification.
DEVIATION_ALERT_PCT = 10.0


@dataclass
class RealisedDay:
    """One calendar day's realised saving from measured battery flows."""

    day: str  # ISO date; a plain str so the Store payload round-trips
    wear_cost_eur_kwh: float
    charged_kwh: float = 0.0
    discharged_kwh: float = 0.0
    charge_cost_eur: float = 0.0
    discharge_value_eur: float = 0.0

    def add_interval(
        self,
        power_w: float,
        dt_hours: float,
        price_eur_kwh: float | None,
    ) -> None:
        """
        Integrate one held power sample over its elapsed interval.

        Sign per the charge loop (ADR-0007): positive W charges the
        battery. Energy with no known price (the ~30 s post-midnight
        window, or a genuine price outage) still counts as energy —
        it just carries no cash value, mirroring the cost sensor.
        """
        if dt_hours <= 0 or power_w == 0:
            return
        energy_kwh = abs(power_w) * dt_hours / 1000.0
        if power_w > 0:
            self.charged_kwh += energy_kwh
            if price_eur_kwh is not None:
                self.charge_cost_eur += energy_kwh * price_eur_kwh
        else:
            self.discharged_kwh += energy_kwh
            if price_eur_kwh is not None:
                self.discharge_value_eur += energy_kwh * price_eur_kwh

    @property
    def realised_eur(self) -> float:
        """Discharge value minus charge cost minus wear on discharge."""
        return (
            self.discharge_value_eur
            - self.charge_cost_eur
            - self.wear_cost_eur_kwh * self.discharged_kwh
        )

    def as_dict(self) -> dict[str, Any]:
        """Store-round-trippable payload."""
        return {
            "day": self.day,
            "wear_cost_eur_kwh": self.wear_cost_eur_kwh,
            "charged_kwh": self.charged_kwh,
            "discharged_kwh": self.discharged_kwh,
            "charge_cost_eur": self.charge_cost_eur,
            "discharge_value_eur": self.discharge_value_eur,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RealisedDay:
        """Rebuild from a Store payload."""
        return cls(
            day=payload["day"],
            wear_cost_eur_kwh=payload.get("wear_cost_eur_kwh", 0.0),
            charged_kwh=payload.get("charged_kwh", 0.0),
            discharged_kwh=payload.get("discharged_kwh", 0.0),
            charge_cost_eur=payload.get("charge_cost_eur", 0.0),
            discharge_value_eur=payload.get("discharge_value_eur", 0.0),
        )


@dataclass
class MonthLedger:
    """One month's daily realised and forecast savings, EUR."""

    month: str  # "YYYY-MM"
    realised_daily: dict[str, float] = field(default_factory=dict)
    forecast_daily: dict[str, float] = field(default_factory=dict)

    def fold_day(self, day: RealisedDay) -> None:
        """Record a closed day's realised saving (same-month days only)."""
        if day.day.startswith(self.month):
            self.realised_daily[day.day] = day.realised_eur

    def record_forecast(self, day: str, eur: float) -> None:
        """Record (or refresh) a day's forecast saving."""
        if day.startswith(self.month):
            self.forecast_daily[day] = eur

    @property
    def realised_eur(self) -> float:
        """Month-to-date realised saving."""
        return sum(self.realised_daily.values())

    @property
    def forecast_eur(self) -> float:
        """Month-to-date forecast saving."""
        return sum(self.forecast_daily.values())

    def deviation_pct(self) -> float | None:
        """Realised vs forecast, %; None while the forecast sum is 0."""
        if self.forecast_eur == 0:
            return None
        return (self.realised_eur - self.forecast_eur) / abs(self.forecast_eur) * 100.0

    def report(self) -> str:
        """Render the monthly reconciliation notification body."""
        deviation = self.deviation_pct()
        lines = [
            f"Battery Opt report for {self.month}:",
            f"- Realised saving (measured battery flows): "
            f"€{self.realised_eur:.2f} over {len(self.realised_daily)} days",
            f"- Forecast saving: €{self.forecast_eur:.2f}",
        ]
        if deviation is None:
            lines.append("- Deviation: n/a (no forecast recorded)")
        else:
            flag = (
                " — ABOVE the 10% reconciliation threshold, investigate"
                if abs(deviation) > DEVIATION_ALERT_PCT
                else ""
            )
            lines.append(f"- Deviation: {deviation:+.1f}%{flag}")
        lines.append(
            "Reconcile the invoice against the cost sensor's monthly "
            "statistic (sensor.battery_opt_cost_today)."
        )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """Store-round-trippable payload."""
        return {
            "month": self.month,
            "realised_daily": dict(self.realised_daily),
            "forecast_daily": dict(self.forecast_daily),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MonthLedger:
        """Rebuild from a Store payload."""
        return cls(
            month=payload["month"],
            realised_daily=dict(payload.get("realised_daily", {})),
            forecast_daily=dict(payload.get("forecast_daily", {})),
        )
