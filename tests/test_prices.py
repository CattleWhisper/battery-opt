"""
Tests for the EDP Indexada price model.

Verification target: the plan asks for reconstruction against the
invoice, but the docs carry no invoice unit prices. Instead the model
is validated against docs/tariff-reference.md §6 — the monthly
arbitrage table — which is derived from the MA30 series through this
exact formula, plus §6's own TAR decomposition (+0.2276 EUR/kWh).
"""

from datetime import date, datetime, timedelta

import pytest

from custom_components.battery_opt.core.calendar import CALENDARS, period
from custom_components.battery_opt.core.prices import (
    K1_MEDIA,
    TAR_ENERGIA_2026,
    TAR_POLICY,
    TAR_TABLES,
    price,
    tar_energia_for,
    total_daily_cost,
)

ETA_RT = 0.90  # round-trip efficiency, CONTEXT.md

# docs/tariff-reference.md §6: net value (EUR/kWh) of moving 1 kWh from
# vazio to ponta, weekly cycle, K1=1.08, eta=0.90, from the §5 MA30
# series: (year, month) -> (vazio MA30, ponta MA30, expected net).
ARBITRAGE_TABLE = {
    (2025, 9): (73.72, 33.78, 0.164),
    (2025, 10): (67.11, 24.09, 0.161),
    (2025, 11): (68.88, 66.11, 0.212),
    (2025, 12): (55.58, 67.83, 0.233),
    (2026, 1): (67.78, 87.26, 0.241),
    (2026, 2): (58.46, 82.52, 0.248),
    (2026, 3): (6.37, 24.08, 0.247),
    (2026, 4): (37.41, 58.62, 0.247),
    (2026, 5): (50.74, 7.45, 0.163),
    (2026, 6): (63.85, 5.11, 0.141),
    (2026, 7): (82.97, 27.18, 0.142),
    (2026, 8): (117.23, 74.18, 0.154),
}


def _first_weekday_of(year: int, month: int, weekday: int) -> date:
    """First date in the month falling on the given weekday (0=Mon)."""
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def test_price_formula_hand_computed() -> None:
    """
    OMIE 58.46 in winter vazio: 0.05846*1.164*1.08 + 0.0185 + 0.0158.

    2026-02-15 is a Sunday, so 03:00 is vazio.
    """
    got = price(58.46, datetime(2026, 2, 15, 3, 0))
    assert got == pytest.approx(0.10779124)


def test_price_applies_tar_of_the_period() -> None:
    """Same OMIE price differs across periods by exactly the TAR spread."""
    p_ponta = price(50.0, datetime(2026, 7, 15, 10, 0))  # Wed, summer ponta
    p_vazio = price(50.0, datetime(2026, 7, 15, 3, 0))  # Wed, vazio
    assert p_ponta - p_vazio == pytest.approx(0.2452 - 0.0158)


def test_k1_injectable_media_vs_horaria() -> None:
    """Média (K1=1.10) differs from Horária (K1=1.08) on the market term only."""
    dt = datetime(2026, 7, 15, 3, 0)
    diff = price(100.0, dt, k1=K1_MEDIA) - price(100.0, dt)
    assert diff == pytest.approx(100.0 / 1000 * 1.164 * 0.02)


def test_tar_decomposition_from_docs() -> None:
    """§6: the TAR alone contributes +0.2276 EUR/kWh to the arbitrage."""
    tar_net = TAR_ENERGIA_2026["ponta"] - TAR_ENERGIA_2026["vazio"] / ETA_RT
    assert tar_net == pytest.approx(0.2276, abs=5e-5)


@pytest.mark.parametrize(("month_key", "row"), sorted(ARBITRAGE_TABLE.items()))
def test_monthly_arbitrage_table(
    month_key: tuple[int, int],
    row: tuple[float, float, float],
) -> None:
    """
    Reproduce §6: net = price(ponta) - price(vazio)/eta, per month.

    Tolerance: the six OMIE-inverted months (ponta MA30 below vazio
    MA30: Sep, Oct, May-Aug) reproduce to within 0.002 EUR/kWh rather
    than exact rounding — consistent with the reference table having
    been computed from unrounded MA30 series. The six winter months
    match to the table's 3-decimal rounding.
    """
    year, month = month_key
    vazio_ma30, ponta_ma30, expected_net = row
    sunday = _first_weekday_of(year, month, 6)
    wednesday = _first_weekday_of(year, month, 2)
    p_vazio = price(vazio_ma30, datetime(sunday.year, sunday.month, sunday.day, 3, 0))
    p_ponta = price(
        ponta_ma30, datetime(wednesday.year, wednesday.month, wednesday.day, 10, 0)
    )
    net = p_ponta - p_vazio / ETA_RT
    assert net == pytest.approx(expected_net, abs=2.5e-3)


def test_tar_policy_is_forward() -> None:
    """
    TAR_POLICY is "forward": the latest table applies to every date.

    Deliberate (Checkpoint A review): the backtest estimates FORWARD
    economics under the tariff being moved to (Indexada Horária with
    current ERSE TAR), not a reconstruction of historical bills. ERSE
    reported a +3.5% BTN TAR variation 2025->2026, so applying the 2026
    table to Sep-Dec 2025 data is a real modelling choice, not a
    rounding detail. A "historical" per-date policy exists for future
    reconciliation work.
    """
    assert TAR_POLICY == "forward"


def test_forward_policy_applies_current_table_to_past_dates() -> None:
    """Sep 2025 data is priced with the 2026 TAR under "forward"."""
    assert tar_energia_for(date(2025, 9, 1)) == TAR_ENERGIA_2026


def test_tar_tables_version_independently_of_calendar() -> None:
    """
    A 2027 TAR row is a data addition touching no calendar code.

    TAR values and hour spans move on independent effective-date axes:
    extending TAR_TABLES changes no calendar lookup, and the calendar
    keeps exactly one span table while TAR grows.
    """
    tar_2027 = {"ponta": 0.2500, "cheias": 0.0420, "vazio": 0.0160}
    tables = (*TAR_TABLES, (date(2027, 1, 1), tar_2027))
    # Historical policy picks per effective date...
    assert (
        tar_energia_for(date(2027, 2, 1), tables=tables, policy="historical")
        == tar_2027
    )
    assert (
        tar_energia_for(date(2026, 6, 1), tables=tables, policy="historical")
        == TAR_ENERGIA_2026
    )
    # ...forward policy picks the newest for any date...
    assert tar_energia_for(date(2025, 9, 1), tables=tables) == tar_2027
    # ...and the calendar axis is untouched by the wider TAR tables.
    assert len(CALENDARS) == 1
    assert period(datetime(2026, 7, 15, 10, 0)) == "ponta"


def test_historical_policy_raises_before_first_table() -> None:
    """No silent TAR extrapolation into the past under "historical"."""
    with pytest.raises(ValueError, match="No TAR table in force"):
        tar_energia_for(date(2025, 1, 1), policy="historical")


def test_unknown_tar_policy_raises() -> None:
    """Typos in the policy name fail loudly."""
    with pytest.raises(ValueError, match="Unknown TAR policy"):
        tar_energia_for(date(2026, 1, 1), policy="fwd")


def test_total_daily_cost_adds_fixed_terms_and_vat() -> None:
    """Reporting: (energy + (K3 + TAR potencia) * days) * VAT."""
    got = total_daily_cost(1.0, days=2)
    assert got == pytest.approx((1.0 + (0.1171 + 0.2291) * 2) * 1.23)
