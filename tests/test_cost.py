"""
Tests for cost.py's CostToday accumulator (plan Task 13 / decision 8).

Pure-arithmetic tests for CostToday; CostTracker's HA wiring (state
tracking, Store persistence, availability) is covered separately in
test_cost_integration.py under the full pytest-homeassistant-custom-
component harness.
"""

from __future__ import annotations

import pytest

from custom_components.battery_opt.core.prices import K3, TAR_POTENCIA_2026
from custom_components.battery_opt.cost import FIXED_EUR_PER_DAY, CostToday


def test_fixed_eur_per_day_matches_k3_plus_tar_potencia() -> None:
    """The daily fixed term is exactly K3 + TAR_POTENCIA_2026 (spec §4)."""
    assert pytest.approx(K3 + TAR_POTENCIA_2026) == FIXED_EUR_PER_DAY


def test_new_day_starts_at_the_fixed_term_only() -> None:
    """Before any energy is seen, total_eur is exactly the fixed term."""
    today = CostToday(day="2026-07-15")
    assert today.variable_eur == 0.0
    assert today.energy_today_kwh == 0.0
    assert today.total_eur == pytest.approx(FIXED_EUR_PER_DAY)


def test_add_delta_accumulates_variable_and_energy() -> None:
    """delta_kwh * price_eur_kwh accumulates into variable_eur."""
    today = CostToday(day="2026-07-15")
    today.add_delta(1.0, 0.15)
    today.add_delta(0.5, 0.30)
    assert today.energy_today_kwh == pytest.approx(1.5)
    assert today.variable_eur == pytest.approx(1.0 * 0.15 + 0.5 * 0.30)
    assert today.total_eur == pytest.approx(today.variable_eur + FIXED_EUR_PER_DAY)


def test_add_delta_with_no_known_price_still_counts_energy() -> None:
    """A priceless delta (prices_ok False at that instant) adds energy, not cost."""
    today = CostToday(day="2026-07-15")
    today.add_delta(2.0, None)
    assert today.energy_today_kwh == pytest.approx(2.0)
    assert today.variable_eur == 0.0


def test_add_delta_ignores_non_positive_deltas() -> None:
    """
    Zero or negative deltas are a no-op inside CostToday.

    The meter-reset floor-to-zero happens in the caller (CostTracker);
    this guards CostToday itself against ever double-booking a reset.
    """
    today = CostToday(day="2026-07-15")
    today.add_delta(0.0, 0.20)
    today.add_delta(-5.0, 0.20)
    assert today.energy_today_kwh == 0.0
    assert today.variable_eur == 0.0


def test_as_dict_from_dict_round_trip() -> None:
    """Store persistence round-trips exactly."""
    today = CostToday(day="2026-07-15", variable_eur=1.23, energy_today_kwh=4.56)
    restored = CostToday.from_dict(today.as_dict())
    assert restored == today


def test_from_dict_tolerates_missing_optional_keys() -> None:
    """A minimal {"day": ...} payload still produces a valid CostToday."""
    restored = CostToday.from_dict({"day": "2026-07-15"})
    assert restored.day == "2026-07-15"
    assert restored.variable_eur == 0.0
    assert restored.energy_today_kwh == 0.0
    assert restored.fixed_eur == pytest.approx(FIXED_EUR_PER_DAY)
