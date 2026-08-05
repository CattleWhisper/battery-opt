"""
Battery driver: a thin layer over `marstek_venus_modbus` (ADR-0004).

This integration is a planner, not a driver. It NEVER opens a Modbus
connection — the Venus E accepts exactly one and `marstek_venus_modbus`
owns it. Every write goes through `hass.services.async_call` against
that integration's entities; the entity ids are chosen by the user in
the config flow (Task 8), never hardcoded.

Zero-export is enforced internally by the device via its smart meter
(open question #3, resolved 2026-08-05); the executor still validates
every plan against C-1..C-7 before actuating — defence in depth.

The module imports nothing from `homeassistant` at runtime: `hass` is
duck-typed (`services.async_call`, `states.get`), which keeps the
driver and its tests runnable without an HA install. Failure policy
per Task 7: each failed call raises `DriverError`; the third
consecutive failure raises `DriverUnavailableError`, which the
executor (Task 9) maps to `healthy=off`. A successful call resets the
counter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Mode = Literal["charge", "discharge", "idle"]

MAX_CONSECUTIVE_FAILURES = 3

# Select-option labels on the marstek_venus_modbus mode entity.
# Placeholder mapping — verify against the real integration during the
# Task 7 manual test (force 500 W charge, confirm on the device) and
# make it configurable in the config flow if the labels differ.
DEFAULT_MODE_OPTIONS: dict[Mode, str] = {
    "charge": "Charge",
    "discharge": "Discharge",
    "idle": "Stop",
}

_BAD_STATES = frozenset({"unavailable", "unknown", "none", ""})


class DriverError(Exception):
    """A driver call failed."""


class DriverUnavailableError(DriverError):
    """Three consecutive failures — the executor must go healthy=off."""


@dataclass(frozen=True)
class MarstekEntities:
    """Entity ids of the marstek_venus_modbus integration, user-chosen."""

    mode_select: str
    power_number: str
    soc_sensor: str


class BatteryDriver(ABC):
    """What the executor needs from a battery: mode, power, SoC."""

    @abstractmethod
    async def set_mode(self, mode: Mode) -> None:
        """Switch the battery between charge, discharge and idle."""

    @abstractmethod
    async def set_power(self, watts: float) -> None:
        """Set the active power setpoint in W."""

    @abstractmethod
    async def read_soc(self) -> float:
        """Return the state of charge in percent (0-100)."""


class MarstekDriver(BatteryDriver):
    """The real driver: service calls only, never Modbus (ADR-0004)."""

    def __init__(
        self,
        hass: Any,  # duck-typed HomeAssistant; no HA import at runtime
        entities: MarstekEntities,
        mode_options: dict[Mode, str] | None = None,
    ) -> None:
        """Bind to a hass instance and the user-configured entities."""
        self._hass = hass
        self._entities = entities
        self._mode_options = (
            DEFAULT_MODE_OPTIONS if mode_options is None else (mode_options)
        )
        self._failures = 0

    def _record_failure(self, cause: BaseException) -> DriverError:
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            return DriverUnavailableError(
                f"{self._failures} consecutive driver failures"
            )
        return DriverError(str(cause))

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self._hass.services.async_call(domain, service, data)
        except Exception as err:  # any failure counts toward the limit
            raise self._record_failure(err) from err
        self._failures = 0

    async def set_mode(self, mode: Mode) -> None:
        """Switch mode via select.select_option on the mode entity."""
        await self._call(
            "select",
            "select_option",
            {
                "entity_id": self._entities.mode_select,
                "option": self._mode_options[mode],
            },
        )

    async def set_power(self, watts: float) -> None:
        """Set the W setpoint via number.set_value on the power entity."""
        await self._call(
            "number",
            "set_value",
            {"entity_id": self._entities.power_number, "value": watts},
        )

    async def read_soc(self) -> float:
        """Read the SoC sensor state; unavailable states are failures."""
        state = self._hass.states.get(self._entities.soc_sensor)
        if state is None or str(state.state).lower() in _BAD_STATES:
            cause = ValueError(f"{self._entities.soc_sensor} unavailable")
            raise self._record_failure(cause) from cause
        try:
            soc = float(state.state)
        except ValueError as err:
            raise self._record_failure(err) from err
        self._failures = 0
        return soc


@dataclass
class FakeDriver(BatteryDriver):
    """In-memory driver for tests: records every call in order."""

    soc_percent: float = 27.0
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def set_mode(self, mode: Mode) -> None:
        """Record the mode change."""
        self.calls.append(("set_mode", mode))

    async def set_power(self, watts: float) -> None:
        """Record the power setpoint."""
        self.calls.append(("set_power", watts))

    async def read_soc(self) -> float:
        """Record the read and return the configured SoC."""
        self.calls.append(("read_soc", None))
        return self.soc_percent
