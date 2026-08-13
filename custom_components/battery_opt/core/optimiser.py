"""
Greedy charge/discharge optimiser (docs/spec.md §7).

Pure function, no Home Assistant imports (ADR-0001). The algorithm
pairs the highest-price interval with remaining discharge capacity
against the cheapest PRECEDING interval with remaining charge
capacity (causality: energy must be stored before it is used), while
the C-8 condition holds:

    price[d] > price[c] / eta_rt + WEAR_COST

Construction guarantees the constraints rather than repairing them
afterwards: causality plus a start-at-or-above-floor SoC keeps C-4
automatic, and each pair is sized against the SoC ceiling over [c, d)
(C-5). Energy already stored above the floor at day start is "free"
(its grid cost is sunk): it is discharged into the highest-priced
intervals whose price beats the wear cost, before any pairing.

Greedy is O(n^2) over 96 intervals — milliseconds. Known limitation
(ADR-0003): pairs are chosen locally; the gap to the LP optimum is
typically <2%, and the achieved saving is not strictly monotone in
capacity (see test_doubling_capacity_never_decreases_saving).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .plan import ARMED_CHARGE_KWH, BatteryParams, Plan

if TYPE_CHECKING:
    from collections.abc import Sequence

# Energy quantum below which an allocation is treated as zero (kWh).
_EPS_KWH = 1e-9
_MIN_PAIR_KWH = 1e-6


@dataclass(frozen=True)
class SolveResult:
    """The plan plus the saving it forecasts vs not cycling."""

    plan: Plan
    forecast_saving_eur: float


@dataclass
class _DayState:
    """Mutable allocation state shared by the two solve phases."""

    prices: Sequence[float]
    params: BatteryParams
    discharge_cap: list[float]  # remaining meter kWh out, C-1/C-2
    charge_cap: list[float]  # remaining grid kWh in, C-3
    charge_e: list[float]
    discharge_e: list[float]
    soc_end: list[float]  # SoC at the END of each interval
    charged: set[int] = field(default_factory=set)  # C-7 bookkeeping
    discharged: set[int] = field(default_factory=set)
    saving: float = 0.0


def _initial_state(
    prices: Sequence[float],
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
) -> _DayState:
    """Build per-interval capacities (C-1..C-3) and the empty allocation."""
    n = len(prices)
    dt = params.interval_hours
    return _DayState(
        prices=prices,
        params=params,
        discharge_cap=[
            min(params.p_discharge_max_w, max(0.0, load_w[i] - solar_w[i])) * dt / 1000
            for i in range(n)
        ],
        charge_cap=[
            max(0.0, min(params.p_charge_max_w, params.p_usable_w - load_w[i]))
            * dt
            / 1000
            for i in range(n)
        ],
        charge_e=[0.0] * n,
        discharge_e=[0.0] * n,
        soc_end=[params.start_soc_kwh] * n,
    )


def _discharge_free_energy(state: _DayState) -> None:
    """
    Step 0: dump energy stored above the floor where price beats wear.

    Its grid cost is sunk (paid on a previous day), so in single-day
    terms every kWh sold above the wear cost is pure gain; holding it
    has no value here.
    """
    params = state.params
    eta = params.eta_one_way
    wear = params.wear_cost_eur_kwh
    n = len(state.prices)
    free_store = params.start_soc_kwh - params.cap_min_kwh  # kWh in the battery
    if free_store <= _EPS_KWH:
        return
    for i in sorted(range(n), key=lambda k: state.prices[k], reverse=True):
        if state.prices[i] <= wear or free_store <= _EPS_KWH:
            break
        q = min(state.discharge_cap[i], free_store * eta)
        if q <= _MIN_PAIR_KWH:
            continue
        state.discharge_e[i] += q
        state.discharge_cap[i] -= q
        state.discharged.add(i)
        free_store -= q / eta
        for t in range(i, n):
            state.soc_end[t] -= q / eta
        state.saving += q * (state.prices[i] - wear)


def _place_pair(state: _DayState, d: int) -> bool:
    """Try to pair discharge interval d with its best preceding charge."""
    params = state.params
    eta = params.eta_one_way
    eta_rt = params.eta_roundtrip
    wear = params.wear_cost_eur_kwh
    by_price_asc = sorted(range(d), key=lambda k: state.prices[k])
    for c in by_price_asc:
        if c in state.discharged or state.charge_cap[c] <= _EPS_KWH:
            continue
        if state.prices[d] <= state.prices[c] / eta_rt + wear:
            return False  # C-8: even the cheapest viable charge fails
        headroom = min(
            params.cap_usable_kwh - state.soc_end[t] for t in range(c, d)
        )  # C-5 over [c, d)
        q = min(state.discharge_cap[d], state.charge_cap[c] * eta_rt, headroom * eta)
        if q <= _MIN_PAIR_KWH:
            continue  # ceiling-blocked from this c; try a dearer one
        grid = q / eta_rt
        state.charge_e[c] += grid
        state.charge_cap[c] -= grid
        state.charged.add(c)
        state.discharge_e[d] += q
        state.discharge_cap[d] -= q
        state.discharged.add(d)
        for t in range(c, d):
            state.soc_end[t] += q / eta
        state.saving += q * (state.prices[d] - state.prices[c] / eta_rt - wear)
        return True
    return False


def _pair_intervals(state: _DayState) -> None:
    """Step 1: greedy pairing (spec §7), highest price d first."""
    n = len(state.prices)
    dead: set[int] = set()
    max_pairs = 4 * n  # safety bound; capacity exhaustion ends it sooner
    for _ in range(max_pairs):
        candidates = [
            i
            for i in range(n)
            if i not in dead
            and i not in state.charged
            and state.discharge_cap[i] > _EPS_KWH
        ]
        if not candidates:
            break
        d = max(candidates, key=lambda k: state.prices[k])
        if not _place_pair(state, d):
            dead.add(d)


def _arm_charge_extensions(state: _DayState) -> None:
    """
    Step 2: hold the CHARGE state past each run while it stays useful.

    Owner decision 2026-08-13 (spec §7): every quarter after a charge
    run that could still profitably feed a remaining discharge (the
    C-8 condition against the cheapest discharge still ahead) is armed
    at ARMED_CHARGE_KWH — a state selector (ADR-0007), not an energy
    claim — shaved off the run's own allocation, latest quarters
    first, so totals and the modelled trajectory are conserved (the
    energy only moves later; C-5 can never be pushed up). Real
    shortfalls (anti-feed discharged a deeper-than-forecast load; the
    charge loop was throttled) then recover in the armed time: the
    loop charges at full power and the firmware percent-target stops
    at ACTUAL full. `state.saving` is adjusted by the exact modelled
    cost delta so it still matches the evaluator; the plan may
    therefore model up to a few 1e-4 EUR of insurance cost.
    """
    params = state.params
    eta_rt = params.eta_roundtrip
    wear = params.wear_cost_eur_kwh
    n = len(state.prices)
    # Cheapest discharge price strictly after each index; None means
    # no discharge remains ahead, which blocks arming there. Prices
    # can be negative (never assume >= 0), so None is the sentinel.
    min_d_after: list[float | None] = [None] * n
    running: float | None = None
    for i in range(n - 1, -1, -1):
        min_d_after[i] = running
        if state.discharge_e[i] > _EPS_KWH:
            running = (
                state.prices[i] if running is None else min(running, state.prices[i])
            )
    i = 0
    while i < n:
        if state.charge_e[i] <= _EPS_KWH:
            i += 1
            continue
        run_start = i
        while i < n and state.charge_e[i] > _EPS_KWH:
            i += 1
        run_end = i  # exclusive
        candidates: list[int] = []
        q = run_end
        while (
            q < n
            and state.discharge_e[q] <= _EPS_KWH
            and state.charge_e[q] <= _EPS_KWH
            and state.charge_cap[q] > ARMED_CHARGE_KWH
            and (ahead := min_d_after[q]) is not None
            and ahead > state.prices[q] / eta_rt + wear
        ):
            candidates.append(q)
            q += 1
        i = max(i, q)  # never re-walk freshly armed quarters as a run
        if not candidates:
            continue
        # How many candidates the run can afford (donors keep at least
        # the armed quantum so they stay charge quarters themselves).
        available = sum(
            max(0.0, state.charge_e[d] - ARMED_CHARGE_KWH)
            for d in range(run_start, run_end)
        )
        candidates = candidates[: int(available / ARMED_CHARGE_KWH)]
        need = ARMED_CHARGE_KWH * len(candidates)
        for c in candidates:
            state.charge_e[c] = ARMED_CHARGE_KWH
            state.charge_cap[c] -= ARMED_CHARGE_KWH
            state.charged.add(c)
            state.saving -= state.prices[c] * ARMED_CHARGE_KWH
        for d in range(run_end - 1, run_start - 1, -1):
            take = min(state.charge_e[d] - ARMED_CHARGE_KWH, need)
            if take <= 0.0:
                continue
            state.charge_e[d] -= take
            state.charge_cap[d] += take
            state.saving += state.prices[d] * take
            need -= take
            if need <= 0.0:
                break


def solve(
    prices: Sequence[float],
    load_w: Sequence[float],
    solar_w: Sequence[float],
    params: BatteryParams,
) -> SolveResult:
    """Compute the day's plan for the given per-interval prices (EUR/kWh)."""
    n = len(prices)
    if len(load_w) != n or len(solar_w) != n:
        msg = f"length mismatch: prices {n}, load {len(load_w)}, solar {len(solar_w)}"
        raise ValueError(msg)
    state = _initial_state(prices, load_w, solar_w, params)
    _discharge_free_energy(state)
    _pair_intervals(state)
    _arm_charge_extensions(state)
    dt = params.interval_hours
    plan = Plan(
        charge_w=tuple(e / dt * 1000 for e in state.charge_e),
        discharge_w=tuple(e / dt * 1000 for e in state.discharge_e),
    )
    return SolveResult(plan=plan, forecast_saving_eur=state.saving)
