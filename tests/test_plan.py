"""
Tests for the shared Plan type, constraint validator and evaluator.

The validator encodes C-1..C-7 from docs/spec.md §6; every strategy
(static, greedy) must produce plans that pass it, and the executor
(Task 9) will run it before each actuation. The evaluator is the
single-day cost function the backtest (Task 6) will reuse.
"""

import itertools
from datetime import date, datetime

import pytest

from custom_components.battery_opt.core.calendar import period
from custom_components.battery_opt.core.plan import (
    BatteryParams,
    Plan,
    price_segments,
    saving_vs_no_cycling,
    schedule_segments,
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


def test_schedule_segments_merges_runs_and_skips_hold() -> None:
    """Consecutive equal quarters merge; hold quarters yield nothing."""
    charge = [0.0, 0.0, 1000.0, 1000.0, 500.0, 0.0, 0.0, 0.0]
    discharge = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 800.0, 800.0]
    segments = schedule_segments(date(2026, 1, 15), charge, discharge)
    assert segments == [
        {
            "start": "2026-01-15T00:30:00+00:00",
            "end": "2026-01-15T01:00:00+00:00",
            "direction": "charge",
            "power_w": 1000.0,
        },
        {
            "start": "2026-01-15T01:00:00+00:00",
            "end": "2026-01-15T01:15:00+00:00",
            "direction": "charge",
            "power_w": 500.0,
        },
        {
            "start": "2026-01-15T01:30:00+00:00",
            "end": "2026-01-15T02:00:00+00:00",
            "direction": "discharge",
            "power_w": 800.0,
        },
    ]


def test_schedule_segments_splits_adjacent_direction_change() -> None:
    """A charge quarter directly followed by discharge splits cleanly."""
    segments = schedule_segments(date(2026, 1, 15), [2500.0, 0.0], [0.0, 1040.0])
    assert [s["direction"] for s in segments] == ["charge", "discharge"]
    assert segments[0]["end"] == segments[1]["start"]


def test_schedule_segments_summer_offset_and_final_interval() -> None:
    """Lisbon summer is +01:00; a run to the end of day closes at 24:00."""
    n = 96
    discharge = [0.0] * n
    discharge[92:] = [1040.0] * 4  # 23:00-24:00
    segments = schedule_segments(date(2026, 8, 7), [0.0] * n, discharge)
    assert segments == [
        {
            "start": "2026-08-07T23:00:00+01:00",
            "end": "2026-08-08T00:00:00+01:00",
            "direction": "discharge",
            "power_w": 1040.0,
        }
    ]


def test_schedule_segments_all_hold_is_empty() -> None:
    """A day with no actions produces an empty schedule."""
    assert schedule_segments(date(2026, 1, 15), [0.0] * 96, [0.0] * 96) == []


def test_schedule_segments_rounds_power_for_display() -> None:
    """Float dust rounds away (and merges) instead of splitting runs."""
    segments = schedule_segments(
        date(2026, 1, 15), [588.8888888888891, 588.8888888888886], [0.0, 0.0]
    )
    assert segments == [
        {
            "start": "2026-01-15T00:00:00+00:00",
            "end": "2026-01-15T00:30:00+00:00",
            "direction": "charge",
            "power_w": 588.9,
        }
    ]


def test_price_segments_merge_equal_quarters_within_a_period() -> None:
    """00:00-02:00 winter vazio: merging is purely on the rounded price."""
    prices = [0.05, 0.05, 0.07, 0.07, 0.07, 0.05, 0.05, 0.05]
    segments = price_segments(date(2026, 1, 15), prices)
    assert [s["price_eur_kwh"] for s in segments] == [0.05, 0.07, 0.05]
    assert all(s["tar_period"] == "vazio" for s in segments)
    assert segments[0]["start"] == "2026-01-15T00:00:00+00:00"
    assert segments[-1]["end"] == "2026-01-15T02:00:00+00:00"


def test_price_segments_flat_day_splits_only_on_tar_boundaries() -> None:
    """A flat delivered price still splits at every TAR period switch."""
    segments = price_segments(date(2026, 8, 7), [0.1] * 96)  # summer Friday
    assert segments[0]["start"] == "2026-08-07T00:00:00+01:00"
    assert segments[-1]["end"] == "2026-08-08T00:00:00+01:00"
    for first, second in itertools.pairwise(segments):
        assert first["end"] == second["start"]  # contiguous, no gaps
        assert first["tar_period"] != second["tar_period"]
    for segment in segments:
        assert segment["price_eur_kwh"] == 0.1
        naive_start = datetime.fromisoformat(str(segment["start"])).replace(tzinfo=None)
        assert segment["tar_period"] == period(naive_start)
    assert len(segments) >= 3  # a summer weekday has ponta, cheias, vazio


def test_soc_trajectory_self_discharge_drains_each_interval() -> None:
    """Owner 2026-08-17: ~19 W standby drain, 4.75 Wh per quarter."""
    params = BatteryParams(soc_start_kwh=4.0, self_discharge_w=19.0)
    hold = Plan(charge_w=(0.0,) * 96, discharge_w=(0.0,) * 96)
    soc = soc_trajectory(hold, params, include_self_discharge=True)
    per_quarter = 19.0 * 0.25 / 1000
    assert soc[1] == pytest.approx(4.0 - per_quarter)
    assert soc[-1] == pytest.approx(4.0 - 96 * per_quarter)


def test_self_discharge_trajectory_clamps_at_the_reserve_floor() -> None:
    """The drained trajectory models the battery defending its floor."""
    params = BatteryParams(soc_start_kwh=1.40, self_discharge_w=19.0)
    hold = Plan(charge_w=(0.0,) * 96, discharge_w=(0.0,) * 96)
    soc = soc_trajectory(hold, params, include_self_discharge=True)
    assert min(soc) >= params.cap_min_kwh - 1e-9
    assert soc[-1] == pytest.approx(params.cap_min_kwh)


def test_soc_trajectory_defaults_to_flow_only() -> None:
    """Validator and optimiser semantics unchanged: drain is opt-in."""
    params = BatteryParams(soc_start_kwh=4.0, self_discharge_w=19.0)
    hold = Plan(charge_w=(0.0,) * 96, discharge_w=(0.0,) * 96)
    assert soc_trajectory(hold, params) == [4.0] * 97
