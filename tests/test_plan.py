"""
Tests for the shared Plan type, constraint validator and evaluator.

The validator encodes C-1..C-7 from docs/spec.md §6; every strategy
(static, greedy) must produce plans that pass it, and the executor
(Task 9) will run it before each actuation. The evaluator is the
single-day cost function the backtest (Task 6) will reuse.
"""

import pytest

from custom_components.battery_opt.core.plan import (
    BatteryParams,
    Plan,
    saving_vs_no_cycling,
    soc_trajectory,
    validate_plan,
)

PARAMS = BatteryParams()


def test_default_params_match_context() -> None:
    """Defaults come from CONTEXT.md, not invented."""
    assert PARAMS.cap_usable_kwh == 5.0
    assert PARAMS.cap_min_kwh == 1.35
    assert PARAMS.p_charge_max_w == 2000
    assert PARAMS.p_discharge_max_w == 2500
    assert PARAMS.p_usable_w == 4400
    assert PARAMS.eta_roundtrip == 0.90
    assert PARAMS.wear_cost_eur_kwh == 0.020


def test_soc_trajectory_charge_step() -> None:
    """Charging 2000 W for 15 min adds 0.5 kWh x eta_c to the SoC."""
    plan = Plan(charge_w=(2000.0, 0.0), discharge_w=(0.0, 0.0))
    soc = soc_trajectory(plan, PARAMS)
    eta_c = 0.90**0.5
    assert soc[0] == pytest.approx(1.35)  # starts at the reserve floor
    assert soc[1] == pytest.approx(1.35 + 0.5 * eta_c)
    assert soc[2] == pytest.approx(1.35 + 0.5 * eta_c)  # idle interval


def test_valid_plan_has_no_violations() -> None:
    """A feasible charge-then-discharge pair validates clean."""
    plan = Plan(charge_w=(2000.0, 0.0), discharge_w=(0.0, 1000.0))
    assert validate_plan(plan, [1040.0, 1040.0], [0.0, 0.0], PARAMS) == []


def test_c1_zero_export_violation() -> None:
    """Discharging above net load would export - flagged as C-1."""
    plan = Plan(charge_w=(0.0,), discharge_w=(1500.0,))
    violations = validate_plan(plan, [1000.0], [0.0], PARAMS)
    assert any(v.startswith("C-1") for v in violations)


def test_c1_uses_net_load_after_solar() -> None:
    """Solar reduces the exportable margin: net = load - solar."""
    plan = Plan(charge_w=(0.0,), discharge_w=(800.0,))
    without_solar = validate_plan(plan, [1000.0], [0.0], PARAMS)
    assert not any(v.startswith("C-1") for v in without_solar)
    with_solar = validate_plan(plan, [1000.0], [500.0], PARAMS)
    assert any(v.startswith("C-1") for v in with_solar)


def test_c2_discharge_power_cap() -> None:
    """Discharge above P_DIS_MAX is flagged even with load to absorb it."""
    plan = Plan(charge_w=(0.0,), discharge_w=(3000.0,))
    violations = validate_plan(plan, [4000.0], [0.0], PARAMS)
    assert any(v.startswith("C-2") for v in violations)


def test_c3_contracted_power_margin() -> None:
    """Charge is capped at P_USABLE minus house load, not P_CHG_MAX."""
    plan = Plan(charge_w=(2000.0,), discharge_w=(0.0,))
    assert validate_plan(plan, [2000.0], [0.0], PARAMS) == []
    violations = validate_plan(plan, [3000.0], [0.0], PARAMS)  # margin 1400
    assert any(v.startswith("C-3") for v in violations)


def test_c4_reserve_floor_violation() -> None:
    """Discharging energy that was never stored breaches the floor."""
    plan = Plan(charge_w=(0.0,), discharge_w=(1000.0,))
    violations = validate_plan(plan, [1040.0], [0.0], PARAMS)
    assert any(v.startswith("C-4") for v in violations)


def test_c5_capacity_ceiling_violation() -> None:
    """Charging past usable capacity is flagged across the trajectory."""
    n = 20  # 5 h at 2000 W stores ~9.5 kWh >> 5.0 usable
    plan = Plan(charge_w=(2000.0,) * n, discharge_w=(0.0,) * n)
    violations = validate_plan(plan, [1040.0] * n, [0.0] * n, PARAMS)
    assert any(v.startswith("C-5") for v in violations)


def test_c7_exclusivity_violation() -> None:
    """Charging and discharging in the same interval is flagged."""
    plan = Plan(charge_w=(500.0,), discharge_w=(500.0,))
    violations = validate_plan(plan, [1040.0], [0.0], PARAMS)
    assert any(v.startswith("C-7") for v in violations)


def test_saving_vs_no_cycling_hand_computed() -> None:
    """
    Charge 0.5 kWh at 0.10, discharge 0.25 kWh at 0.30, wear 0.02.

    saving = 0.30*0.25 - 0.10*0.5 - 0.02*0.25 = 0.075 - 0.05 - 0.005.
    """
    plan = Plan(charge_w=(2000.0, 0.0), discharge_w=(0.0, 1000.0))
    saving = saving_vs_no_cycling(plan, [0.10, 0.30], PARAMS)
    assert saving == pytest.approx(0.02)
