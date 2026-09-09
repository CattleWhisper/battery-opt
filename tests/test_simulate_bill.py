"""
Tests for the offline bill simulation (backtest/simulate_bill.py).

The break-even OMIE this tool prints is the number an actual tariff
switch gets decided on, so it is pinned by a self-consistency
property rather than a golden value: priced at exactly the reported
break-even, the two tariffs must cost the same. Meter handling is
tested against the integration's own rule (cost.py, decision 8) — a
negative delta is a reset and counts as ZERO, never as negative
consumption.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from backtest.simulate_bill import (
    TAR_ENERGIA_SIMPLES,
    load_meter,
    load_prices,
    replay,
)
from custom_components.battery_opt.core.prices import (
    K1_HORARIA,
    K2,
    PERDAS,
    TAR_ENERGIA_2026,
    price,
)

if TYPE_CHECKING:
    from pathlib import Path

TZ = ZoneInfo("Europe/Lisbon")
UTC = ZoneInfo("UTC")
DAY = datetime(2026, 9, 1, tzinfo=TZ)
SIMPLES_RATE = 0.1420


def _write_prices(directory: Path, omie_eur_mwh: float, days: int = 1) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for offset in range(days):
        day = DAY + timedelta(days=offset)
        points, delivered = [], []
        for quarter in range(96):
            moment = day + timedelta(minutes=15 * quarter)
            delivered.append(price(omie_eur_mwh, moment.replace(tzinfo=None)))
            points.append({"start": moment.isoformat(), "price": omie_eur_mwh})
        (directory / f"{day.date().isoformat()}.json").write_text(
            json.dumps(
                {
                    "date": day.date().isoformat(),
                    "padded": False,
                    "omie_eur_mwh": points,
                    "delivered_eur_kwh": delivered,
                }
            ),
            encoding="utf-8",
        )


def _write_meter(path: Path, readings: list[tuple[datetime, float]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entity_id", "state", "last_changed"])
        for moment, value in readings:
            writer.writerow(
                [
                    "sensor.meter",
                    f"{value}",
                    moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                ]
            )


def _flat_day(kwh_per_quarter: float, start: float = 0.0) -> list:
    return [
        (DAY + timedelta(minutes=15 * q), start + kwh_per_quarter * q)
        for q in range(97)
    ]


def _breakeven_omie(kwh_by_period: dict[str, float]) -> float:
    """Restate the tool's own formula independently, for the test."""
    total = sum(kwh_by_period.values())
    tar = sum(kwh_by_period[p] * TAR_ENERGIA_2026[p] for p in kwh_by_period)
    slope = (1 + PERDAS) * K1_HORARIA / 1000.0
    return (SIMPLES_RATE - K2 - tar / total) / slope


def test_at_the_breakeven_omie_both_tariffs_cost_the_same(tmp_path: Path) -> None:
    """
    The property the switching decision rests on.

    Priced at the break-even, Indexada's measured cost must equal the
    Simples cost for the same kWh — otherwise the number the tool
    prints is not a break-even at all.
    """
    meter = tmp_path / "meter.csv"
    _write_meter(meter, _flat_day(0.26))
    readings = load_meter(meter)

    # First pass at an arbitrary price, only to learn the period mix.
    _write_prices(tmp_path / "p0", 100.0, days=2)
    mix = replay(readings, load_prices(tmp_path / "p0"), None, None)
    breakeven = _breakeven_omie(dict(mix.kwh_by_period))

    _write_prices(tmp_path / "p1", breakeven, days=2)
    totals = replay(readings, load_prices(tmp_path / "p1"), None, None)

    assert totals.unpriced_kwh == 0.0  # every delta priced, or the test is vacuous
    assert totals.indexada_eur == pytest.approx(totals.kwh * SIMPLES_RATE, rel=1e-9)


def test_a_meter_reset_counts_as_zero_not_negative(tmp_path: Path) -> None:
    """
    Decision 8's rule, mirrored: a daily utility_meter is a valid input.

    The reset drop contributes nothing; the climb after it is counted
    in full, so only the sliver at the reset instant is lost.
    """
    meter = tmp_path / "meter.csv"
    _write_meter(
        meter,
        [
            (DAY + timedelta(hours=1), 5.0),
            (DAY + timedelta(hours=2), 8.0),  # +3
            (DAY + timedelta(hours=3), 0.0),  # reset, ignored
            (DAY + timedelta(hours=4), 2.0),  # +2
        ],
    )
    _write_prices(tmp_path / "p", 100.0)
    totals = replay(load_meter(meter), load_prices(tmp_path / "p"), None, None)
    assert totals.kwh == pytest.approx(5.0)


def test_unparseable_states_are_dropped(tmp_path: Path) -> None:
    """`unavailable` / `unknown` rows are skipped, not treated as zero."""
    meter = tmp_path / "meter.csv"
    with meter.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entity_id", "state", "last_changed"])
        for value in ("1.0", "unavailable", "3.0", "unknown", "4.0"):
            writer.writerow(["sensor.meter", value, DAY.isoformat()])
    assert [value for _, value in load_meter(meter)] == [1.0, 3.0, 4.0]


def test_energy_is_attributed_to_the_right_tar_period(tmp_path: Path) -> None:
    """A summer weekday's 09:15-12:15 block is ponta; the night is vazio."""
    meter = tmp_path / "meter.csv"
    _write_meter(meter, _flat_day(0.25))
    _write_prices(tmp_path / "p", 100.0)
    totals = replay(load_meter(meter), load_prices(tmp_path / "p"), None, None)

    # 2026-09-01 is a summer Tuesday: 3 h ponta, 7 h vazio (00:00-07:00).
    assert totals.kwh_by_period["ponta"] == pytest.approx(3 * 4 * 0.25)
    assert totals.kwh_by_period["vazio"] == pytest.approx(7 * 4 * 0.25)
    assert sum(totals.kwh_by_period.values()) == pytest.approx(totals.kwh)


def test_tri_horaria_beats_simples_on_tar_for_a_low_ponta_profile() -> None:
    """
    The project's actual thesis, on the owner's measured shape.

    The 2 Aug - 1 Sep 2026 invoice measured only 4.4% of consumption
    in ponta; at that mix the tri-horaria TAR must come out BELOW the
    flat Simples TAR, or switching has no regulated-side upside at all.
    """
    invoice = {"vazio": 330.0, "cheias": 371.0, "ponta": 32.0}
    total = sum(invoice.values())
    tar_tri = sum(kwh * TAR_ENERGIA_2026[p] for p, kwh in invoice.items())
    tar_simples = total * TAR_ENERGIA_SIMPLES
    assert tar_tri < tar_simples
    assert tar_simples - tar_tri == pytest.approx(16.1, abs=0.2)
