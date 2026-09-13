"""
Tests for the UPS-mode plan builder (core/ups.py, ADR-0011).

The mode makes one promise to the owner — the battery will still be
there when the grid is not — so the tests are written against that
promise rather than against the arithmetic. Three properties carry it:
the plan can never discharge, the charge window does not shrink when
the model thinks the pack is full, and the resulting plan passes the
same C-1..C-7 validation as every other plan (an invalid plan is
refused by the executor, which in this mode means the reserve quietly
stops being maintained).

`core/` is HA-free (ADR-0001), so these are plain unit tests.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from custom_components.battery_opt.core.plan import (
    ARMED_CHARGE_KWH,
    BatteryParams,
    soc_trajectory,
    validate_plan,
)
from custom_components.battery_opt.core.prices import TAR_ENERGIA_2026
from custom_components.battery_opt.core.ups import (
    ARMING_HEADROOM_KWH,
    tar_rank,
    ups_plan,
    ups_start_soc_kwh,
)

N = 96
# A winter weekday: ponta 09:30-12:00 and 18:30-21:00 (docs/tariff-
# reference.md §3), so a TAR-ranked plan has somewhere expensive to
# avoid.
WINTER_WEEKDAY = date(2026, 1, 14)
PARAMS = BatteryParams()
LOAD = [1040.0] * N  # CONTEXT.md base load
SOLAR = [0.0] * N


def _floor_seeded(params: BatteryParams = PARAMS) -> BatteryParams:
    """Params seeded at the reserve floor: an empty pack."""
    return dataclasses.replace(params, soc_start_kwh=params.cap_min_kwh)


def _full_seeded(params: BatteryParams = PARAMS) -> BatteryParams:
    """Params seeded as full as a UPS seed is ever allowed to be."""
    return dataclasses.replace(
        params, soc_start_kwh=ups_start_soc_kwh(params.cap_usable_kwh, params)
    )


def _cheap_first_half() -> list[float]:
    """Prices that make the first half of the day obviously cheapest."""
    return [0.05 if i < N // 2 else 0.40 for i in range(N)]


def test_a_ups_plan_never_discharges() -> None:
    """
    The defining guarantee: no interval, no price, no seed.

    Given prices that would make arbitrage irresistible — a cheap
    night and a very expensive afternoon — the optimiser would sell.
    This must not.
    """
    plan = ups_plan(WINTER_WEEKDAY, LOAD, _floor_seeded(), _cheap_first_half())
    assert set(plan.discharge_w) == {0.0}


def test_a_ups_plan_never_discharges_even_when_the_pack_is_full() -> None:
    """A full pack plus a dear afternoon is the strongest temptation."""
    plan = ups_plan(WINTER_WEEKDAY, LOAD, _full_seeded(), _cheap_first_half())
    assert set(plan.discharge_w) == {0.0}


def test_the_plan_validates_from_an_empty_pack() -> None:
    """C-1..C-7 hold, so the executor will actuate it."""
    params = _floor_seeded()
    plan = ups_plan(WINTER_WEEKDAY, LOAD, params, _cheap_first_half())
    assert validate_plan(plan, LOAD, SOLAR, params) == []


def test_the_plan_validates_from_a_full_pack() -> None:
    """
    The steady state of the mode, and the one that used to break C-5.

    Once the model believes the pack is full it allocates no energy,
    yet every window interval is still ARMED — energy added on top of
    a full pack. `ARMING_HEADROOM_KWH` plus the seed clamp is what
    keeps that under the ceiling; without them the mode would fail
    validation precisely once it had done its job, and the executor
    fails closed on an invalid plan.
    """
    params = _full_seeded()
    plan = ups_plan(WINTER_WEEKDAY, LOAD, params, _cheap_first_half())
    assert validate_plan(plan, LOAD, SOLAR, params) == []
    assert max(soc_trajectory(plan, params)) <= params.cap_usable_kwh


def test_charging_lands_in_the_cheapest_intervals() -> None:
    """Selection is by price, not by clock."""
    plan = ups_plan(WINTER_WEEKDAY, LOAD, _floor_seeded(), _cheap_first_half())
    charging = [i for i, w in enumerate(plan.charge_w) if w > 0]
    assert charging  # something was planned
    assert all(i < N // 2 for i in charging)


def test_the_charge_window_does_not_shrink_when_the_model_thinks_it_is_full() -> None:
    """
    Seed-independence: the CHARGE state spans the same intervals either way.

    This is what lets the run-time pair (charge loop + the firmware's
    percent target) top up a pack whose real SoC sits below the
    model's. Without it, a UPS that had drifted high would stop
    charging and stay short — invisibly, until the grid failed.
    """
    prices = _cheap_first_half()
    empty = ups_plan(WINTER_WEEKDAY, LOAD, _floor_seeded(), prices)
    full = ups_plan(WINTER_WEEKDAY, LOAD, _full_seeded(), prices)
    assert [w > 0 for w in full.charge_w] == [w > 0 for w in empty.charge_w]


def test_a_full_pack_books_no_real_energy() -> None:
    """
    Armed, but not claiming energy it does not need.

    The window is held open (previous test) at ARMED_CHARGE_KWH — a
    state selector per ADR-0007 — so the modelled import stays
    negligible while the state still says CHARGE.
    """
    params = _full_seeded()
    plan = ups_plan(WINTER_WEEKDAY, LOAD, params, _cheap_first_half())
    armed_w = ARMED_CHARGE_KWH / params.interval_hours * 1000
    assert {round(w, 9) for w in plan.charge_w} <= {0.0, round(armed_w, 9)}


def test_an_empty_pack_is_filled_to_capacity() -> None:
    """The point of the mode: end the charge window essentially full."""
    params = _floor_seeded()
    plan = ups_plan(WINTER_WEEKDAY, LOAD, params, _cheap_first_half())
    end = soc_trajectory(plan, params)[-1]
    # Short of the ceiling only by the arming headroom held back for
    # the state selectors — 1 Wh, not a shortfall the pack can resolve.
    assert end <= params.cap_usable_kwh
    assert end >= params.cap_usable_kwh - 2 * ARMING_HEADROOM_KWH


def test_the_contracted_power_ceiling_caps_every_charge() -> None:
    """
    C-3: load plus charging never exceeds the usable ceiling.

    Uses a house load high enough that the device limit is not the
    binding constraint, so the test actually exercises the ceiling.
    """
    params = dataclasses.replace(
        _floor_seeded(), p_usable_w=3000.0, p_charge_max_w=2500.0
    )
    load = [1800.0] * N
    plan = ups_plan(WINTER_WEEKDAY, load, params, _cheap_first_half())
    assert validate_plan(plan, load, SOLAR, params) == []
    assert max(plan.charge_w) <= params.p_usable_w - 1800.0 + 1e-6


def test_a_day_with_no_charging_headroom_plans_nothing() -> None:
    """A house already at the ceiling gets an all-hold plan, not a crash."""
    params = _floor_seeded()
    load = [params.p_usable_w] * N
    plan = ups_plan(WINTER_WEEKDAY, load, params, _cheap_first_half())
    assert set(plan.charge_w) == {0.0}
    assert validate_plan(plan, load, SOLAR, params) == []


def test_without_prices_tar_keeps_charging_out_of_ponta() -> None:
    """
    The offline fallback still refuses the day's most expensive band.

    The executor builds this plan when no price vector exists at all
    (startup, a publication gap). Ranking by TAR is what stops a
    priceless day from filling the reserve at 0.2452 EUR/kWh.
    """
    params = _floor_seeded()
    plan = ups_plan(WINTER_WEEKDAY, LOAD, params)
    ranks = tar_rank(WINTER_WEEKDAY, N, params)
    charging = [i for i, w in enumerate(plan.charge_w) if w > 0]
    assert charging
    assert all(ranks[i] == TAR_ENERGIA_2026["vazio"] for i in charging)


def test_tar_rank_follows_the_calendar() -> None:
    """00:00-07:00 is vazio, 09:30-12:00 ponta on a winter weekday."""
    ranks = tar_rank(WINTER_WEEKDAY, N, PARAMS)
    assert ranks[0] == TAR_ENERGIA_2026["vazio"]  # 00:00
    assert ranks[38] == TAR_ENERGIA_2026["ponta"]  # 09:30
    assert ranks[47] == TAR_ENERGIA_2026["ponta"]  # 11:45
    assert ranks[48] == TAR_ENERGIA_2026["cheias"]  # 12:00


def test_tied_prices_charge_as_early_as_possible() -> None:
    """
    A flat day restores the reserve now, not at midnight.

    Ties are the norm under TAR ranking, where a whole vazio stretch
    shares one value; for a backup asset the earliest of them is the
    right one.
    """
    plan = ups_plan(WINTER_WEEKDAY, LOAD, _floor_seeded(), [0.10] * N)
    charging = [i for i, w in enumerate(plan.charge_w) if w > 0]
    assert charging == list(range(len(charging)))


def test_the_seed_is_the_floor_when_nothing_was_measured() -> None:
    """No SoC sensor: assume empty, which only over-books charging."""
    assert ups_start_soc_kwh(None, PARAMS) == pytest.approx(PARAMS.cap_min_kwh)


def test_the_seed_is_held_below_capacity_so_arming_fits() -> None:
    """A 100% reading still leaves room for the armed window."""
    seed = ups_start_soc_kwh(PARAMS.cap_usable_kwh, PARAMS)
    assert seed == pytest.approx(PARAMS.cap_usable_kwh - ARMING_HEADROOM_KWH)
    assert seed < PARAMS.cap_usable_kwh


def test_the_seed_never_falls_below_the_reserve_floor() -> None:
    """
    A reading under the floor cannot seed a C-4 violation.

    The battery defends its own floor (ADR-0008), so a reading below
    it means the model, not the pack, is wrong.
    """
    assert ups_start_soc_kwh(0.0, PARAMS) == pytest.approx(PARAMS.cap_min_kwh)


def test_a_measured_seed_is_used_as_given() -> None:
    """Between the floor and the clamp, ADR-0010's measurement stands."""
    assert ups_start_soc_kwh(3.0, PARAMS) == pytest.approx(3.0)
