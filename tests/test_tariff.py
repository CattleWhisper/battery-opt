"""Tests for the supply-contract model (core/tariff.py, ADR-0012)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.battery_opt.core.calendar import (
    CALENDARS,
    DAILY_CALENDARS,
    period,
)
from custom_components.battery_opt.core.prices import (
    K1_HORARIA,
    K1_MEDIA,
    TAR_BI_FORA_DE_VAZIO_2026,
    TAR_BI_VAZIO_2026,
    TAR_ENERGIA_2026,
    TAR_SIMPLES_2026,
    bi_table,
    flat_table,
    price,
    tar_energia_for,
    tri_table,
)
from custom_components.battery_opt.core.tariff import (
    DEFAULT_TARIFF,
    INDEXADA_DAILY_FIXED_EUR,
    Tariff,
)

SUMMER_MONDAY = date(2026, 6, 15)
WINTER_MONDAY = date(2026, 1, 12)


def _week(start: date) -> list[datetime]:
    """Every quarter of the 7 days from `start`, naive local time."""
    midnight = datetime(start.year, start.month, start.day)
    return [midnight + timedelta(minutes=15 * q) for q in range(7 * 96)]


@pytest.mark.parametrize("start", [SUMMER_MONDAY, WINTER_MONDAY])
def test_default_tariff_reproduces_the_pre_adr_0012_price_exactly(
    start: date,
) -> None:
    """Indexada Horária tri semanal is bit-for-bit what production priced."""
    for i, when in enumerate(_week(start)):
        omie = -40.0 + (i % 37) * 9.5  # includes negatives
        assert DEFAULT_TARIFF.quarter_price(omie, when) == price(omie, when)


def test_default_tariff_matches_on_aware_cet_instants() -> None:
    """Production feeds CET-aware starts; the default must convert like price()."""
    when = datetime(2026, 6, 15, 8, 30, tzinfo=UTC)  # 09:30 Lisbon: ponta
    assert DEFAULT_TARIFF.quarter_price(55.0, when) == price(55.0, when)
    assert DEFAULT_TARIFF.group_of(when) == "ponta"


def test_default_tariff_identity() -> None:
    """The default is Horária tri semanal with EDP's Indexada terms."""
    assert DEFAULT_TARIFF.key == "indexada_horaria/tri/semanal"
    assert DEFAULT_TARIFF.k1 == K1_HORARIA
    assert DEFAULT_TARIFF.daily_fixed_eur == INDEXADA_DAILY_FIXED_EUR
    assert DEFAULT_TARIFF.calendars is CALENDARS
    assert not DEFAULT_TARIFF.is_flat
    assert DEFAULT_TARIFF.needs_omie


def test_media_defaults_to_its_own_k1() -> None:
    """EDP Indexada Média carries K1 = 1.10."""
    assert Tariff(pricing="indexada_media", option="tri").k1 == K1_MEDIA


@pytest.mark.parametrize(
    ("pricing", "option", "flat"),
    [
        ("fixa", "simples", True),
        ("indexada_media", "simples", True),
        ("indexada_horaria", "simples", False),  # OMIE still moves
        ("fixa", "bi", False),
        ("fixa", "tri", False),
        ("indexada_media", "bi", False),
        ("indexada_media", "tri", False),
        ("indexada_horaria", "bi", False),
        ("indexada_horaria", "tri", False),
    ],
)
def test_only_a_flat_price_is_flat(pricing: str, option: str, *, flat: bool) -> None:
    """UPS is forced exactly where no quarter is cheaper than another."""
    prices = {"simples": flat_table(0.14), "bi": bi_table(0.1, 0.2)}.get(
        option, tri_table(0.1, 0.2, 0.3)
    )
    tariff = Tariff(
        pricing=pricing,  # type: ignore[arg-type]
        option=option,  # type: ignore[arg-type]
        energy_by_period=prices if pricing == "fixa" else None,
    )
    assert tariff.is_flat is flat


def test_fixa_needs_prices_and_no_omie() -> None:
    """A fixed contract is unpriceable without its prices, and needs no OMIE."""
    with pytest.raises(ValueError, match="energy_by_period"):
        Tariff(pricing="fixa", option="tri")
    tariff = Tariff(pricing="fixa", option="tri", energy_by_period=tri_table(1, 2, 3))
    assert not tariff.needs_omie


def test_fixa_prices_by_period_and_ignores_omie() -> None:
    """Fixa tri: the contract price of the instant's period, whatever OMIE did."""
    tariff = Tariff(
        pricing="fixa", option="tri", energy_by_period=tri_table(0.1, 0.2, 0.3)
    )
    ponta = datetime(2026, 6, 15, 10, 0)
    vazio = datetime(2026, 6, 15, 3, 0)
    assert tariff.fixed_price(ponta) == 0.3
    assert tariff.quarter_price(999.0, ponta) == 0.3
    assert tariff.quarter_price(-50.0, vazio) == 0.1


def test_bi_groups_ponta_and_cheias_as_fora_de_vazio() -> None:
    """Bi-horária shows two groups; the daily cycle moves vazio to 22-08."""
    bi = Tariff(pricing="indexada_horaria", option="bi", cycle="diario")
    assert bi.calendars is DAILY_CALENDARS
    assert bi.group_of(datetime(2026, 6, 14, 7, 30)) == "vazio"  # Sunday, daily
    assert bi.group_of(datetime(2026, 6, 14, 11, 0)) == "fora_de_vazio"
    assert bi.discharge_periods == {"ponta", "cheias"}


def test_simples_has_one_group_and_nothing_to_discharge_into() -> None:
    """Simples: one group, no discharge period."""
    simples = Tariff(pricing="indexada_horaria", option="simples")
    assert {simples.group_of(q) for q in _week(SUMMER_MONDAY)} == {"simples"}
    assert simples.discharge_periods == frozenset()
    assert simples.key == "indexada_horaria/simples/-"


def test_indexed_tar_follows_the_option() -> None:
    """Each option's TAR energia reaches the delivered price."""
    when = datetime(2026, 6, 15, 10, 0)  # summer weekday ponta
    base = price(0.0, when, tar_energia=flat_table(0.0))
    expected = {
        "simples": TAR_SIMPLES_2026,
        "bi": TAR_BI_FORA_DE_VAZIO_2026,
        "tri": TAR_ENERGIA_2026["ponta"],
    }
    for option, tar in expected.items():
        tariff = Tariff(pricing="indexada_horaria", option=option)  # type: ignore[arg-type]
        assert tariff.quarter_price(0.0, when) == pytest.approx(base + tar)


def test_tar_energia_for_selects_the_option_series() -> None:
    """The per-option TAR series are versioned like the tri one."""
    assert tar_energia_for(date(2026, 6, 1)) == TAR_ENERGIA_2026
    assert tar_energia_for(date(2026, 6, 1), option="bi") == bi_table(
        TAR_BI_VAZIO_2026, TAR_BI_FORA_DE_VAZIO_2026
    )
    assert tar_energia_for(date(2026, 6, 1), option="simples") == flat_table(
        TAR_SIMPLES_2026
    )


def test_media_averages_per_option_group() -> None:
    """Média tri averages per period; bi pools ponta with cheias; simples all."""
    week = _week(SUMMER_MONDAY)
    points = [(q, float(i % 11)) for i, q in enumerate(week)]

    tri = Tariff(pricing="indexada_media", option="tri")
    averages = tri.period_averages(points)
    assert set(averages) == {"ponta", "cheias", "vazio"}
    ponta = [omie for q, omie in points if period(q) == "ponta"]
    assert averages["ponta"] == pytest.approx(sum(ponta) / len(ponta))

    bi = Tariff(pricing="indexada_media", option="bi")
    fora = [omie for q, omie in points if period(q) != "vazio"]
    assert bi.period_averages(points)["fora_de_vazio"] == pytest.approx(
        sum(fora) / len(fora)
    )

    simples = Tariff(pricing="indexada_media", option="simples")
    assert simples.period_averages(points) == {
        "simples": pytest.approx(sum(o for _, o in points) / len(points))
    }


def test_media_prices_at_the_average_not_the_quarter() -> None:
    """A Média quarter ignores its own OMIE and uses the group's average."""
    tariff = Tariff(pricing="indexada_media", option="tri")
    when = datetime(2026, 6, 15, 10, 0)  # ponta
    averages = {"ponta": 80.0, "cheias": 60.0, "vazio": 40.0}
    expected = price(80.0, when, k1=K1_MEDIA)
    assert tariff.quarter_price(5.0, when, averages) == pytest.approx(expected)
    assert tariff.quarter_price(500.0, when, averages) == pytest.approx(expected)
    with pytest.raises(ValueError, match="omie_averages"):
        tariff.quarter_price(5.0, when)


def test_media_simples_is_flat_across_the_day() -> None:
    """Média simples: one price for every quarter — why it forces UPS."""
    tariff = Tariff(pricing="indexada_media", option="simples")
    averages = {"simples": 70.0}
    prices = {
        tariff.quarter_price(float(i), q, averages)
        for i, q in enumerate(_week(SUMMER_MONDAY))
    }
    assert len(prices) == 1


def test_rates_rank_fixa_by_contract_and_indexed_by_tar() -> None:
    """`rates` is the no-OMIE ranking: contract price, or TAR energia."""
    fixa = Tariff(pricing="fixa", option="bi", energy_by_period=bi_table(0.09, 0.19))
    assert fixa.rates(SUMMER_MONDAY) == bi_table(0.09, 0.19)
    assert DEFAULT_TARIFF.rates(SUMMER_MONDAY) == TAR_ENERGIA_2026
