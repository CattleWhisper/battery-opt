"""
Static schedule and UPS ranking under every tariff (ADR-0012).

The default tariff must reproduce the pre-ADR-0012 schedule exactly —
that is what keeps the Checkpoint C soak untouched — and every other
option must keep the invariants while discharging into its own dear
periods.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from custom_components.battery_opt.core.calendar import (
    DAILY_CALENDARS,
    interval_starts,
    period,
)
from custom_components.battery_opt.core.plan import BatteryParams, validate_plan
from custom_components.battery_opt.core.prices import bi_table, flat_table
from custom_components.battery_opt.core.static_schedule import (
    chained_start_soc,
    is_active_day,
    static_plan,
)
from custom_components.battery_opt.core.tariff import DEFAULT_TARIFF, Tariff
from custom_components.battery_opt.core.ups import tar_rank, ups_plan

PARAMS = BatteryParams()
N = 96
FLAT_LOAD = [1040.0] * N
NO_SOLAR = [0.0] * N

BI_SEMANAL = Tariff(pricing="indexada_horaria", option="bi")
TRI_DIARIO = Tariff(pricing="indexada_horaria", option="tri", cycle="diario")
BI_DIARIO_FIXA = Tariff(
    pricing="fixa", option="bi", cycle="diario", energy_by_period=bi_table(0.1, 0.2)
)
SIMPLES = Tariff(pricing="indexada_horaria", option="simples")


def _days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=d) for d in range(count)]


# Two full years cover both seasons, both DST switches and every weekday.
YEAR_DAYS = _days(date(2025, 1, 1), 730)


def test_default_tariff_reproduces_the_pre_adr_0012_schedule() -> None:
    """Every day of two years: identical plans with and without the tariff."""
    for day in YEAR_DAYS:
        assert static_plan(
            day, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=DEFAULT_TARIFF
        ) == static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS), day


def test_default_tariff_reproduces_the_chained_seed() -> None:
    """The day-chaining seed is unchanged too (weekends skipped as before)."""
    drained = dataclasses.replace(PARAMS, self_discharge_w=19.0)
    for day in _days(date(2026, 6, 1), 28) + _days(date(2026, 1, 5), 28):
        assert chained_start_soc(
            day, FLAT_LOAD, NO_SOLAR, drained, tariff=DEFAULT_TARIFF
        ) == chained_start_soc(day, FLAT_LOAD, NO_SOLAR, drained), day


def test_interval_starts_are_wall_clock_on_a_normal_day() -> None:
    """Off DST days the starts are plain midnight + 15 min steps."""
    day = date(2026, 6, 15)
    midnight = datetime(2026, 6, 15)
    assert interval_starts(day, N) == [
        midnight + timedelta(minutes=15 * q) for q in range(N)
    ]


def test_interval_starts_skip_the_lost_hour_in_march() -> None:
    """29 Mar 2026: 92 quarters; quarter 4 starts at 02:00 wall clock."""
    starts = interval_starts(date(2026, 3, 29), 92)
    assert starts[3] == datetime(2026, 3, 29, 0, 45)
    assert starts[4] == datetime(2026, 3, 29, 2, 0)
    assert starts[-1] == datetime(2026, 3, 29, 23, 45)


@pytest.mark.parametrize("tariff", [BI_SEMANAL, TRI_DIARIO, BI_DIARIO_FIXA])
def test_every_option_discharges_only_into_its_dear_periods(tariff: Tariff) -> None:
    """Discharge only in the tariff's discharge periods; charge only outside them."""
    for day in _days(date(2026, 1, 5), 14) + _days(date(2026, 6, 15), 14):
        plan = static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=tariff)
        for i, when in enumerate(interval_starts(day, N)):
            name = period(when, tariff.calendars)
            if plan.discharge_w[i] > 0:
                assert name in tariff.discharge_periods, (day, when)
            if plan.charge_w[i] > 0:
                assert name not in tariff.discharge_periods, (day, when)


@pytest.mark.parametrize("tariff", [BI_SEMANAL, TRI_DIARIO, BI_DIARIO_FIXA])
def test_every_option_keeps_the_invariants(tariff: Tariff) -> None:
    """C-1..C-7 hold from the floor and from the chained seed."""
    for day in _days(date(2026, 1, 5), 14) + _days(date(2026, 6, 15), 14):
        for seed in (
            None,
            chained_start_soc(day, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=tariff),
        ):
            params = dataclasses.replace(PARAMS, soc_start_kwh=seed)
            plan = static_plan(day, FLAT_LOAD, NO_SOLAR, params, tariff=tariff)
            assert validate_plan(plan, FLAT_LOAD, NO_SOLAR, params) == [], day


def test_bi_charges_overnight_even_in_summer() -> None:
    """Bi-horária's midday is fora de vazio: it charges in vazio all year."""
    plan = static_plan(
        date(2026, 6, 16), FLAT_LOAD, NO_SOLAR, PARAMS, tariff=BI_SEMANAL
    )
    charged = [i for i, w in enumerate(plan.charge_w) if w > 0]
    assert charged
    assert all(i * 0.25 < 7.0 for i in charged)
    assert sum(plan.discharge_w) > 0


def test_bi_weekly_saturday_discharges_and_sunday_does_nothing() -> None:
    """Weekly Saturday has fora de vazio; Sunday is all vazio."""
    saturday, sunday = date(2026, 1, 10), date(2026, 1, 11)
    assert is_active_day(saturday, PARAMS, tariff=BI_SEMANAL)
    assert not is_active_day(sunday, PARAMS, tariff=BI_SEMANAL)
    assert (
        sum(
            static_plan(
                saturday, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=BI_SEMANAL
            ).discharge_w
        )
        > 0
    )
    sunday_plan = static_plan(sunday, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=BI_SEMANAL)
    assert sum(sunday_plan.charge_w) == sum(sunday_plan.discharge_w) == 0


def test_daily_cycle_works_every_day_including_weekends() -> None:
    """Daily cycle: Saturday and Sunday have ponta, so they are active days."""
    for day in _days(date(2026, 1, 5), 7):
        assert is_active_day(day, PARAMS, tariff=TRI_DIARIO), day
        plan = static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=TRI_DIARIO)
        assert sum(plan.discharge_w) > 0, day


def test_daily_winter_overnight_charge_uses_vazio_until_eight() -> None:
    """Daily vazio runs to 08:00: the overnight window may use 07:00-08:00."""
    load = [3000.0] * N  # little headroom: the window needs every vazio quarter
    plan = static_plan(date(2026, 1, 15), load, NO_SOLAR, PARAMS, tariff=TRI_DIARIO)
    charged_hours = {i * 0.25 for i, w in enumerate(plan.charge_w) if w > 0}
    assert any(7.0 <= h < 8.0 for h in charged_hours)
    assert all(h < 8.0 for h in charged_hours)


def test_daily_dst_sunday_keeps_wall_clock_periods() -> None:
    """29 Mar 2026 (92 quarters): discharge lands in the real ponta hours."""
    day = date(2026, 3, 29)
    n = 92
    params = dataclasses.replace(PARAMS, soc_start_kwh=PARAMS.cap_usable_kwh)
    plan = static_plan(day, [1040.0] * n, [0.0] * n, params, tariff=TRI_DIARIO)
    for i, when in enumerate(interval_starts(day, n)):
        if plan.discharge_w[i] > 0:
            assert period(when, DAILY_CALENDARS) == "ponta", when
    assert sum(plan.discharge_w) > 0


def test_simples_has_no_static_arbitrage() -> None:
    """Simples: nothing to discharge into, so the static plan idles; seed = floor."""
    for day in _days(date(2026, 1, 5), 7):
        plan = static_plan(day, FLAT_LOAD, NO_SOLAR, PARAMS, tariff=SIMPLES)
        assert sum(plan.charge_w) == sum(plan.discharge_w) == 0
    assert (
        chained_start_soc(date(2026, 1, 6), FLAT_LOAD, NO_SOLAR, PARAMS, tariff=SIMPLES)
        == PARAMS.cap_min_kwh
    )


def test_tar_rank_uses_the_tariffs_calendar_and_rates() -> None:
    """Offline UPS ranking follows the contract, not the tri weekly default."""
    sunday = date(2026, 1, 11)  # weekly: all vazio; daily: has ponta
    weekly = tar_rank(sunday, N, PARAMS)
    daily = tar_rank(sunday, N, PARAMS, tariff=TRI_DIARIO)
    assert len(set(weekly)) == 1
    assert len(set(daily)) == 3

    fixa = Tariff(pricing="fixa", option="bi", energy_by_period=bi_table(0.09, 0.19))
    assert set(tar_rank(date(2026, 1, 12), N, PARAMS, tariff=fixa)) == {0.09, 0.19}


def test_flat_ups_charges_from_the_start_of_the_day() -> None:
    """A flat tariff ties every interval: the reserve refills as early as possible."""
    flat = Tariff(pricing="fixa", option="simples", energy_by_period=flat_table(0.14))
    plan = ups_plan(date(2026, 1, 12), FLAT_LOAD, PARAMS, tariff=flat)
    first = next(i for i, w in enumerate(plan.charge_w) if w > 0)
    assert first == 0
    assert sum(plan.discharge_w) == 0
