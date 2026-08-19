"""
Battery driver: the ADR-0006 state machine over `marstek_modbus` (ADR-0004).

This integration is a planner, not a driver. It NEVER opens a Modbus
connection — the Venus E accepts exactly one and `marstek_modbus`
owns it. Every write goes through `hass.services.async_call` against
that integration's entities; the entity ids are chosen by the user in
the config flow, never hardcoded.

Three battery states (ADR-0006, spec §8), each a different mechanism:

- CHARGE — external control: force-charge plus a power setpoint. The
  charge-to-SoC backstop written on entry is the load-bearing stop if
  this integration dies mid-window: the kill-test found no firmware
  watchdog (bench finding, 2026-08-11).
- HOLD — external control: force-mode standby. Persists because
  nothing clears external control (no watchdog).
- DISCHARGE — the firmware's anti-feed mode: external control is
  released and the work mode asserted, every time — entering force
  mode flips the work mode back to manual (confirmed on-device).

Discharge is NEVER a power setpoint: force-discharge exports whenever
house load drops below the setpoint. The firmware's anti-feed tracking
is the only mechanism with native zero-export.

The driver does NOT read the SoC (owner decision 2026-08-07): the
reserve floor is the battery's to manage — the firmware discharge
cutoff where the register exists, the device's own internal minimum
otherwise. The integration plans with the floor (C-4) but never
polices it at run time.

The module imports nothing from `homeassistant` at runtime: `hass` is
duck-typed (`services.async_call`, `states.get`), which keeps the
driver and its tests runnable without an HA install. Failure policy
per Task 7: each failed call raises `DriverError`; the third
consecutive failure raises `DriverUnavailableError`, which the
executor maps to `healthy=off`. A successful call resets the counter.
A failure mid-transition leaves the driver state unknown, so the next
`set_state` replays the full sequence instead of the short path.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

BatteryState = Literal["charge", "hold", "discharge"]

MAX_CONSECUTIVE_FAILURES = 3

# Option labels verified against ViperRNMC/marstek_venus_modbus
# registers/e_v3.yaml — the select entities expose the YAML keys
# verbatim. force_mode (42010): standby/charge/discharge;
# user_work_mode (43000): manual/anti_feed/trade_mode. Note:
# force_mode, rs485_control_mode, set_charge_power and charge_to_soc
# ship disabled by default in that integration; the user must enable
# them in HA before setup. user_work_mode is enabled by default.
FORCE_STANDBY = "standby"
FORCE_CHARGE = "charge"
WORK_MODE_ANTI_FEED = "anti_feed"

# charge_to_soc (42011) is a 10-100 % number in the upstream entity.
_CHARGE_TO_SOC_MIN = 10.0
_CHARGE_TO_SOC_MAX = 100.0

_LOGGER = logging.getLogger(__name__)


class DriverError(Exception):
    """A driver call failed."""


class DriverUnavailableError(DriverError):
    """Three consecutive failures — the executor must go healthy=off."""


@dataclass(frozen=True)
class MarstekEntities:
    """
    Entity ids of the marstek_modbus integration, user-chosen.

    The three optional ids degrade gracefully when unset: no
    charge-to-SoC backstop and no setup-time cutoff writes. On the
    Venus E V3 the cutoff numbers are in the upstream register map's
    MISSING list (not created at all), so unset is the expected state
    there — the executor's SoC floor guard is the primary protection.
    """

    mode_select: str
    charge_power_number: str
    rs485_switch: str
    work_mode_select: str
    charge_to_soc_number: str | None = None
    charge_cutoff_number: str | None = None
    discharge_cutoff_number: str | None = None


class BatteryDriver(ABC):
    """What the executor needs from a battery (ADR-0006 states)."""

    @abstractmethod
    async def set_state(
        self,
        state: BatteryState,
        *,
        charge_power_w: float | None = None,
        target_soc_pct: float | None = None,
    ) -> None:
        """Transition to a battery state (idempotent on repeats)."""

    @abstractmethod
    async def set_charge_power(self, watts: float) -> None:
        """Update the charge setpoint without leaving CHARGE."""

    @abstractmethod
    async def write_soc_cutoffs(self, floor_pct: float, ceiling_pct: float) -> bool:
        """
        Write the firmware SOC cutoffs once, at setup. Never fatal.

        Returns True only if both cutoffs are confirmed written (or
        already held the target value — the registers are EEPROM-backed,
        so equal values are never rewritten).
        """


class MarstekDriver(BatteryDriver):
    """The real driver: service calls only, never Modbus (ADR-0004)."""

    def __init__(
        self,
        hass: Any,  # duck-typed HomeAssistant; no HA import at runtime
        entities: MarstekEntities,
    ) -> None:
        """Bind to a hass instance and the user-configured entities."""
        self._hass = hass
        self._entities = entities
        self._failures = 0
        # None = unknown (startup, or a transition failed midway):
        # the next set_state replays the full sequence.
        self._state: BatteryState | None = None

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

    async def _external_control(self, *, engaged: bool) -> None:
        await self._call(
            "switch",
            "turn_on" if engaged else "turn_off",
            {"entity_id": self._entities.rs485_switch},
        )

    async def _force_mode(self, option: str) -> None:
        await self._call(
            "select",
            "select_option",
            {"entity_id": self._entities.mode_select, "option": option},
        )

    async def set_state(
        self,
        state: BatteryState,
        *,
        charge_power_w: float | None = None,
        target_soc_pct: float | None = None,
    ) -> None:
        """Run the spec §8 transition sequence into `state`."""
        if state == self._state:
            return
        previous, self._state = self._state, None
        if state == "charge":
            if charge_power_w is None:
                msg = "CHARGE requires charge_power_w"
                raise ValueError(msg)
            # From HOLD external control is already engaged (spec §8:
            # internal changes are single writes); from DISCHARGE or
            # unknown, engage it first.
            if previous != "hold":
                await self._external_control(engaged=True)
            await self.set_charge_power(charge_power_w)
            await self._write_charge_to_soc(target_soc_pct)
            await self._force_mode(FORCE_CHARGE)
        elif state == "hold":
            if previous != "charge":
                await self._external_control(engaged=True)
            await self._force_mode(FORCE_STANDBY)
        else:  # discharge
            await self._force_mode(FORCE_STANDBY)
            await self._external_control(engaged=False)
            # Re-asserted on EVERY entry: entering force mode flips the
            # work mode back to manual (spec §8, verify item 2).
            await self._call(
                "select",
                "select_option",
                {
                    "entity_id": self._entities.work_mode_select,
                    "option": WORK_MODE_ANTI_FEED,
                },
            )
        self._state = state

    async def _write_charge_to_soc(self, target_soc_pct: float | None) -> None:
        # Backstop against the integration dying mid-window; gated on
        # the entity being configured at all (checklist item 3).
        if target_soc_pct is None or self._entities.charge_to_soc_number is None:
            return
        value = min(_CHARGE_TO_SOC_MAX, max(_CHARGE_TO_SOC_MIN, target_soc_pct))
        await self._call(
            "number",
            "set_value",
            {"entity_id": self._entities.charge_to_soc_number, "value": value},
        )

    async def set_charge_power(self, watts: float) -> None:
        """Set the charge W setpoint via number.set_value."""
        await self._call(
            "number",
            "set_value",
            {"entity_id": self._entities.charge_power_number, "value": watts},
        )

    async def write_soc_cutoffs(self, floor_pct: float, ceiling_pct: float) -> bool:
        """
        Setup-time cutoff writes: compare-before-write, never fatal.

        Outside the three-strike counter on purpose — a missing or
        write-rejecting cutoff entity (upstream marks both MISSING on
        the Venus E V3) must not poison actuation health.
        """
        ok = True
        targets = (
            (self._entities.discharge_cutoff_number, floor_pct, "floor"),
            (self._entities.charge_cutoff_number, ceiling_pct, "ceiling"),
        )
        for entity_id, value, label in targets:
            if entity_id is None:
                _LOGGER.info(
                    "SOC %s cutoff entity not configured; relying on the "
                    "integration-level guard (expected on Venus E V3)",
                    label,
                )
                ok = False
                continue
            current = self._hass.states.get(entity_id)
            try:
                if current is not None and float(current.state) == value:
                    continue  # EEPROM-backed: never rewrite an equal value
            except ValueError:
                pass
            try:
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity_id, "value": value},
                )
            except Exception:  # noqa: BLE001 - never fatal by contract
                _LOGGER.warning(
                    "SOC %s cutoff write to %s rejected; relying on the "
                    "integration-level guard",
                    label,
                    entity_id,
                )
                ok = False
        return ok


def allocate_charge_w(
    total_w: float,
    caps_kwh: Sequence[float],
    *,
    unit_max_w: float,
    step_w: float = 50.0,
) -> list[float]:
    """
    Split a total charge setpoint in capacity-proportional shares.

    ADR-0009: `unit_setpoint = total x cap_unit / sum(caps)` — equal
    C-rate keeps the fleet's SoC percentages in lockstep. A share
    exceeding the unit's device max is capped there and the excess
    redistributed among the units with headroom (at most N rounds).
    Each final share floors DOWN to the register step — rounding up
    could breach the contracted margin (C-3), same rule as the loop's
    total. Zero-capacity entries are treated as equal weights, never a
    division by zero.
    """
    epsilon = 1e-9  # allocation slack, far below any physical watt
    n = len(caps_kwh)
    if n == 0 or total_w <= 0:
        return [0.0] * n
    shares = [0.0] * n
    active = set(range(n))
    remaining = min(total_w, unit_max_w * n)
    while remaining > epsilon and active:
        weight = sum(caps_kwh[i] for i in active)
        tentative = {
            i: (
                remaining * caps_kwh[i] / weight
                if weight > 0
                else remaining / len(active)
            )
            for i in active
        }
        overflow = 0.0
        for i, add in tentative.items():
            allowed = min(add, unit_max_w - shares[i])
            shares[i] += allowed
            overflow += add - allowed
            if shares[i] >= unit_max_w - epsilon:
                active.discard(i)
        if overflow <= epsilon:
            break
        remaining = overflow
    return [int(share // step_w) * step_w for share in shares]


class FleetDriver(BatteryDriver):
    """
    Drive N units as one battery (ADR-0009, Task 16).

    States broadcast to every unit (each keeps its own three-strike
    counter and commanded-state cache, so a failed unit replays its
    own full transition next time). Charge power splits in
    capacity-proportional shares via `allocate_charge_w`. Every unit
    is always attempted — a transient failure on one must not leave
    another stuck in the previous state — and the WORST error raises
    afterwards: any `DriverUnavailableError` makes the whole fleet
    unavailable (the executor's all-healthy gate; with no firmware
    watchdog, a half-commanded fleet is worse than a stopped one).
    N=1 delegates 1:1 and behaves exactly like the bare unit driver.
    """

    def __init__(
        self,
        units: Sequence[BatteryDriver],
        caps_kwh: Sequence[float],
        *,
        unit_max_w: float,
    ) -> None:
        """Bind the unit drivers and their capacities (same order)."""
        if len(units) != len(caps_kwh) or not units:
            msg = "FleetDriver needs one capacity per unit, at least one unit"
            raise ValueError(msg)
        self._units = list(units)
        self._caps_kwh = list(caps_kwh)
        self._unit_max_w = unit_max_w

    def _raise_worst(self, errors: list[DriverError]) -> None:
        if not errors:
            return
        for error in errors:
            if isinstance(error, DriverUnavailableError):
                raise error
        raise errors[0]

    async def set_state(
        self,
        state: BatteryState,
        *,
        charge_power_w: float | None = None,
        target_soc_pct: float | None = None,
    ) -> None:
        """Broadcast the transition; charge power splits per unit."""
        shares: list[float | None]
        if state == "charge":
            if charge_power_w is None:
                msg = "CHARGE requires charge_power_w"
                raise ValueError(msg)
            shares = list(
                allocate_charge_w(
                    charge_power_w, self._caps_kwh, unit_max_w=self._unit_max_w
                )
            )
        else:
            shares = [None] * len(self._units)
        errors: list[DriverError] = []
        for unit, share in zip(self._units, shares, strict=True):
            try:
                await unit.set_state(
                    state, charge_power_w=share, target_soc_pct=target_soc_pct
                )
            except DriverError as err:
                errors.append(err)
        self._raise_worst(errors)

    async def set_charge_power(self, watts: float) -> None:
        """Split the total setpoint in capacity-proportional shares."""
        shares = allocate_charge_w(watts, self._caps_kwh, unit_max_w=self._unit_max_w)
        errors: list[DriverError] = []
        for unit, share in zip(self._units, shares, strict=True):
            try:
                await unit.set_charge_power(share)
            except DriverError as err:
                errors.append(err)
        self._raise_worst(errors)

    async def write_soc_cutoffs(self, floor_pct: float, ceiling_pct: float) -> bool:
        """Write the cutoffs on every unit; True only if all confirmed."""
        results = [
            await unit.write_soc_cutoffs(floor_pct, ceiling_pct) for unit in self._units
        ]
        return all(results)


@dataclass
class FakeDriver(BatteryDriver):
    """In-memory driver for tests: records every call in order."""

    calls: list[tuple[str, object]] = field(default_factory=list)

    async def set_state(
        self,
        state: BatteryState,
        *,
        charge_power_w: float | None = None,
        target_soc_pct: float | None = None,
    ) -> None:
        """Record the state transition."""
        self.calls.append(("set_state", (state, charge_power_w, target_soc_pct)))

    async def set_charge_power(self, watts: float) -> None:
        """Record the charge setpoint."""
        self.calls.append(("set_charge_power", watts))

    async def write_soc_cutoffs(self, floor_pct: float, ceiling_pct: float) -> bool:
        """Record the cutoff write and report success."""
        self.calls.append(("write_soc_cutoffs", (floor_pct, ceiling_pct)))
        return True
