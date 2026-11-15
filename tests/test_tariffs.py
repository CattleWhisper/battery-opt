"""
Tests for the tariff catalogue and its parameters file (backtest/tariffs.py).

The comparison ranks contracts, so a mistake here silently ranks the
wrong one first. Two kinds of error are pinned:

- **Calendar.** The daily cycle is new data, so it gets the same
  treatment docs/tariff-reference.md §4 demands of the weekly one:
  weekly totals (the test that catches structural errors) plus every
  boundary. Bi-horária is checked against the invoice-validated weekly
  calendar rather than against itself.
- **Pricing.** The dynamic tariffs must be the integration's own
  formula, not a lookalike, and every tariff must go unpriced on
  exactly the quarters OMIE is missing.
- **The parameters file.** It must refuse to run on anything missing
  or mistyped — listing every problem at once — because a comparison
  that silently dropped or defaulted a value would rank against a
  guess. The shipped file's sourced values are pinned to core.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from backtest.tariffs import (
    DAILY_CALENDARS,
    DEFAULT_TARIFFS_FILE,
    INDEXADA_DAILY_FIXED_EUR,
    TAR_BI_FORA_DE_VAZIO_2026,
    TAR_BI_VAZIO_2026,
    TariffFileError,
    bi_table,
    dynamic,
    fixed,
    flat_table,
    load_tariffs,
    tri_table,
)
from custom_components.battery_opt.core.calendar import period, weekly_hours
from custom_components.battery_opt.core.prices import (
    K1_MEDIA,
    K3,
    TAR_POTENCIA_2026,
    price,
)

if TYPE_CHECKING:
    from pathlib import Path

# Weeks Monday-to-Sunday, well inside each season.
SUMMER_MONDAY = date(2026, 7, 13)
WINTER_MONDAY = date(2026, 1, 12)
OMIE = 80.0  # EUR/MWh; any value — the checks are on differences
FIXED = 0.3462  # a daily fixed term; pricing tests do not depend on it


def _quarters(monday: date) -> list[datetime]:
    """Every quarter-hour start of a Monday-to-Sunday week, naive local."""
    start = datetime(monday.year, monday.month, monday.day)
    return [start + timedelta(minutes=15 * i) for i in range(7 * 96)]


# --- daily cycle calendar -------------------------------------------


@pytest.mark.parametrize("season", ["summer", "winter"])
def test_daily_cycle_weekly_totals(season: str) -> None:
    """
    10 h vazio, 4 h ponta, 10 h cheias — every day of the week.

    The identity tariff-reference.md §5 uses for the daily Simples
    column ((10 x vazio + 10 x cheias + 4 x ponta) / 24) only holds if
    these are the durations.
    """
    totals = weekly_hours(season, DAILY_CALENDARS)
    assert totals == {"vazio": 70.0, "ponta": 28.0, "cheias": 70.0}


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        # Winter (docs/tariff-reference.md §3, daily cycle)
        (datetime(2026, 1, 14, 7, 59), "vazio"),
        (datetime(2026, 1, 14, 8, 0), "cheias"),
        (datetime(2026, 1, 14, 9, 0), "ponta"),
        (datetime(2026, 1, 14, 10, 29), "ponta"),
        (datetime(2026, 1, 14, 10, 30), "cheias"),
        (datetime(2026, 1, 14, 18, 0), "ponta"),
        (datetime(2026, 1, 14, 20, 29), "ponta"),
        (datetime(2026, 1, 14, 20, 30), "cheias"),
        (datetime(2026, 1, 14, 22, 0), "vazio"),
        # Summer
        (datetime(2026, 7, 15, 10, 29), "cheias"),
        (datetime(2026, 7, 15, 10, 30), "ponta"),
        (datetime(2026, 7, 15, 12, 59), "ponta"),
        (datetime(2026, 7, 15, 13, 0), "cheias"),
        (datetime(2026, 7, 15, 19, 30), "ponta"),
        (datetime(2026, 7, 15, 20, 59), "ponta"),
        (datetime(2026, 7, 15, 21, 0), "cheias"),
        (datetime(2026, 7, 15, 21, 59), "cheias"),
        (datetime(2026, 7, 15, 22, 0), "vazio"),
    ],
)
def test_daily_cycle_boundaries(when: datetime, expected: str) -> None:
    """Each span edge, on both sides."""
    assert period(when, DAILY_CALENDARS) == expected


@pytest.mark.parametrize("monday", [SUMMER_MONDAY, WINTER_MONDAY])
def test_daily_cycle_is_the_same_every_day_of_the_week(monday: date) -> None:
    """
    No weekend: Saturday and Sunday carry ponta like any weekday.

    The whole difference from the weekly cycle, and what makes it
    expensive for a house that is home at weekends.
    """
    week = _quarters(monday)
    by_day = [
        [period(q, DAILY_CALENDARS) for q in week[d * 96 : (d + 1) * 96]]
        for d in range(7)
    ]
    assert all(day == by_day[0] for day in by_day)
    assert "ponta" in by_day[6]  # Sunday


def test_daily_cycle_follows_the_legal_season_switch() -> None:
    """Summer hours begin on the last Sunday of March, as for the weekly cycle."""
    assert period(datetime(2026, 3, 28, 10, 45), DAILY_CALENDARS) == "cheias"
    assert period(datetime(2026, 3, 29, 10, 45), DAILY_CALENDARS) == "ponta"


# --- bi-horária ------------------------------------------------------


@pytest.mark.parametrize("cycle", ["semanal", "diario"])
@pytest.mark.parametrize("monday", [SUMMER_MONDAY, WINTER_MONDAY])
def test_bi_horaria_vazio_is_exactly_the_tri_horaria_vazio(
    cycle: str, monday: date
) -> None:
    """
    Every quarter of a week: the cheap bi-horária TAR iff tri-horária vazio.

    This is the whole justification for bi-horária reusing the
    tri-horária calendar, checked quarter by quarter — on the weekly
    cycle against the invoice-validated `core.calendar.CALENDARS`.
    """
    tri = dynamic("tri", cycle, FIXED)
    bi = dynamic("bi", cycle, FIXED)
    for quarter in _quarters(monday):
        is_vazio = tri.period_of(quarter) == "vazio"
        expected = TAR_BI_VAZIO_2026 if is_vazio else TAR_BI_FORA_DE_VAZIO_2026
        assert bi.energy_by_period[bi.period_of(quarter)] == expected


def test_bi_horaria_prices_ponta_and_cheias_alike() -> None:
    """Fora de vazio carries one TAR, so ponta and cheias cost the same."""
    bi = dynamic("bi", "semanal", FIXED)
    ponta = bi.quarter_price(OMIE, datetime(2026, 1, 14, 10, 0))
    cheias = bi.quarter_price(OMIE, datetime(2026, 1, 14, 14, 0))
    vazio = bi.quarter_price(OMIE, datetime(2026, 1, 14, 3, 0))
    assert ponta == pytest.approx(cheias)
    assert cheias - vazio == pytest.approx(
        TAR_BI_FORA_DE_VAZIO_2026 - TAR_BI_VAZIO_2026
    )


# --- pricing ---------------------------------------------------------


def test_dynamic_tri_semanal_is_the_integrations_own_price() -> None:
    """
    Not a lookalike formula: identical to core.prices.price for a whole week.

    This is the tariff the integration actually prices, so the
    comparison's reference row must agree with the cost sensor to the
    last digit.
    """
    tariff = dynamic("tri", "semanal", FIXED)
    for quarter in _quarters(WINTER_MONDAY):
        assert tariff.quarter_price(OMIE, quarter) == price(OMIE, quarter)


def test_dynamic_tariffs_share_the_commodity_and_differ_only_in_tar() -> None:
    """Same OMIE, same K1/K2/losses: the cycle and option move only the TAR."""
    when = datetime(2026, 1, 14, 3, 0)  # vazio in every option and cycle
    prices = {
        tariff.name: tariff.quarter_price(OMIE, when)
        for tariff in (
            dynamic(o, c, FIXED) for o in ("tri", "bi") for c in ("semanal", "diario")
        )
    }
    assert len({round(p, 12) for p in prices.values()}) == 1


def test_fixed_simples_ignores_the_market() -> None:
    """A fixed offer's price does not move with OMIE."""
    simples = fixed("simples", flat_table(0.1420), FIXED)
    when = datetime(2026, 1, 14, 10, 0)
    assert simples.quarter_price(5.0, when) == pytest.approx(0.1420)
    assert simples.quarter_price(500.0, when) == pytest.approx(0.1420)


def test_fixed_tri_prices_each_period() -> None:
    """Values land on their own period, not shuffled."""
    tri = fixed("tri", tri_table(vazio=0.1, cheias=0.2, ponta=0.3), FIXED)
    assert tri.quarter_price(OMIE, datetime(2026, 1, 14, 3, 0)) == pytest.approx(0.1)
    assert tri.quarter_price(OMIE, datetime(2026, 1, 14, 14, 0)) == pytest.approx(0.2)
    assert tri.quarter_price(OMIE, datetime(2026, 1, 14, 10, 0)) == pytest.approx(0.3)


def test_fixed_bi_diario_uses_the_daily_calendar() -> None:
    """A Sunday 10:00 is vazio weekly but fora de vazio daily."""
    sunday_ten = datetime(2026, 1, 18, 10, 0)
    table = bi_table(vazio=0.1, fora_de_vazio=0.2)
    assert fixed("bi", table, FIXED, "semanal").quarter_price(OMIE, sunday_ten) == 0.1
    assert fixed("bi", table, FIXED, "diario").quarter_price(OMIE, sunday_ten) == 0.2


@pytest.mark.parametrize(
    "tariff",
    [dynamic("tri", "semanal", FIXED), fixed("simples", flat_table(0.1420), FIXED)],
    ids=["dynamic", "fixed"],
)
def test_every_tariff_goes_unpriced_where_omie_is_missing(tariff: object) -> None:
    """
    Fixed offers included: all tariffs must price the SAME quarters.

    Otherwise a publication gap costs an indexed tariff nothing and a
    fixed one its full price, tilting the ranking toward indexed.
    """
    assert tariff.quarter_price(None, datetime(2026, 1, 14, 10, 0)) is None


def test_an_indexed_offer_can_override_its_coefficients() -> None:
    """K1 from the file reaches the formula (e.g. Indexada Média, K1 1.10)."""
    when = datetime(2026, 1, 14, 3, 0)
    horaria = dynamic("tri", "semanal", FIXED)
    media = dynamic("tri", "semanal", FIXED, k1=K1_MEDIA)
    assert media.quarter_price(OMIE, when) == pytest.approx(
        price(OMIE, when, k1=K1_MEDIA)
    )
    assert media.quarter_price(OMIE, when) > horaria.quarter_price(OMIE, when)


# --- the parameters file ---------------------------------------------

_VALID = """
reference = "fixa simples"
detailed = "din tri sem"

[[tariff]]
name = "fixa simples"
pricing = "fixa"
option = "simples"
fixed_eur_day = 0.30
prices = { simples = 0.1420 }

[[tariff]]
name = "fixa bi"
pricing = "fixa"
option = "bi"
cycle = "diario"
fixed_eur_day = 0.31
prices = { vazio = 0.10, fora_de_vazio = 0.20 }

[[tariff]]
name = "fixa tri"
pricing = "fixa"
option = "tri"
cycle = "semanal"
fixed_eur_day = 0.32
prices = { vazio = 0.10, cheias = 0.20, ponta = 0.30 }

[[tariff]]
name = "din tri sem"
pricing = "indexada"
option = "tri"
cycle = "semanal"
fixed_eur_day = 0.3462

[[tariff]]
name = "media bi dia"
pricing = "indexada"
option = "bi"
cycle = "diario"
fixed_eur_day = 0.3462
k1 = 1.10
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tariffs.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _problems(tmp_path: Path, text: str) -> str:
    with pytest.raises(TariffFileError) as caught:
        load_tariffs(_write(tmp_path, text))
    return str(caught.value)


def test_a_complete_file_loads_every_tariff(tmp_path: Path) -> None:
    """Every field lands where it belongs, including each fixed term."""
    loaded = load_tariffs(_write(tmp_path, _VALID))
    tariffs = {t.name: t for t in loaded.tariffs}
    assert list(tariffs) == [
        "fixa simples",
        "fixa bi",
        "fixa tri",
        "din tri sem",
        "media bi dia",
    ]
    assert loaded.reference == "fixa simples"
    assert loaded.detailed == "din tri sem"
    assert {t.daily_fixed_eur for t in loaded.tariffs} == {0.30, 0.31, 0.32, 0.3462}
    assert tariffs["fixa bi"].cycle == "diario"
    assert tariffs["fixa bi"].energy_by_period == bi_table(0.10, 0.20)
    assert tariffs["fixa tri"].energy_by_period == tri_table(0.10, 0.20, 0.30)
    assert tariffs["din tri sem"].indexed
    assert tariffs["media bi dia"].k1 == pytest.approx(1.10)


def test_every_todo_is_reported_at_once(tmp_path: Path) -> None:
    """Fix-one-rerun-fix-the-next would be miserable: list them all."""
    text = _VALID.replace("fixed_eur_day = 0.30", 'fixed_eur_day = "TODO"').replace(
        "cheias = 0.20", 'cheias = "TODO"'
    )
    problems = _problems(tmp_path, text)
    assert '"fixa simples": fixed_eur_day is TODO' in problems
    assert '"fixa tri": prices.cheias is TODO' in problems
    assert "2 problem(s)" in problems


def test_a_missing_fixed_term_is_refused(tmp_path: Path) -> None:
    """The fixed term is required for every tariff; no default exists."""
    text = _VALID.replace("fixed_eur_day = 0.31\n", "")
    assert '"fixa bi": fixed_eur_day must be a number' in _problems(tmp_path, text)


def test_a_misspelt_key_is_refused_not_ignored(tmp_path: Path) -> None:
    """`fixed_eur_dia` silently ignored would leave the real field missing."""
    text = _VALID.replace("fixed_eur_day = 0.32", "fixed_eur_dia = 0.32")
    problems = _problems(tmp_path, text)
    assert "unknown key 'fixed_eur_dia'" in problems


def test_fixed_prices_must_match_the_option(tmp_path: Path) -> None:
    """A bi-horária offer given a ponta price is a mistake, not extra data."""
    text = _VALID.replace(
        "prices = { vazio = 0.10, fora_de_vazio = 0.20 }",
        "prices = { vazio = 0.10, fora_de_vazio = 0.20, ponta = 0.30 }",
    )
    assert "prices for option bi must be exactly" in _problems(tmp_path, text)


def test_bi_and_tri_need_a_cycle_but_simples_does_not(tmp_path: Path) -> None:
    """The cycle changes a bi/tri bill; simples has no periods to place."""
    text = _VALID.replace(
        'cycle = "semanal"\nfixed_eur_day = 0.32', "fixed_eur_day = 0.32"
    )
    problems = _problems(tmp_path, text)
    assert '"fixa tri": cycle must be one of' in problems
    assert "fixa simples" not in problems


def test_indexed_offers_take_no_prices_and_fixed_ones_no_coefficients(
    tmp_path: Path,
) -> None:
    """Each family's keys stay in their family."""
    text = _VALID.replace(
        'fixed_eur_day = 0.3462\n\n[[tariff]]\nname = "media',
        "fixed_eur_day = 0.3462\nprices = { simples = 0.1 }\n\n"
        '[[tariff]]\nname = "media',
    ).replace(
        "prices = { simples = 0.1420 }", "prices = { simples = 0.1420 }\nk1 = 1.1"
    )
    problems = _problems(tmp_path, text)
    assert '"din tri sem": indexada takes no prices' in problems
    assert '"fixa simples": k1 only applies to pricing indexada' in problems


def test_names_must_be_unique_and_references_must_exist(tmp_path: Path) -> None:
    """Two rows called the same thing would make the "vs" column ambiguous."""
    text = _VALID.replace('name = "fixa bi"', 'name = "fixa tri"').replace(
        'detailed = "din tri sem"', 'detailed = "nope"'
    )
    problems = _problems(tmp_path, text)
    assert 'tariff name "fixa tri" is used 2 times' in problems
    assert "detailed 'nope' must name one of the tariffs" in problems


def test_negative_and_non_numeric_values_are_refused(tmp_path: Path) -> None:
    """A typo'd "0,142" or a sign slip must not reach the simulation."""
    text = _VALID.replace("simples = 0.1420", 'simples = "0,142"').replace(
        "fixed_eur_day = 0.31", "fixed_eur_day = -0.31"
    )
    problems = _problems(tmp_path, text)
    assert "prices.simples must be a number" in problems
    assert "fixed_eur_day must not be negative" in problems


SHIPPED_FILES = sorted(DEFAULT_TARIFFS_FILE.parent.glob("tariffs*.toml"))


@pytest.mark.parametrize("path", SHIPPED_FILES, ids=lambda p: p.name)
def test_every_shipped_parameters_file_loads(path: Path) -> None:
    """
    The files are the owner's to edit; what is pinned is that they RUN.

    Their contents change as offers are added or dropped, so no test
    fixes which tariffs they hold — only that a typo cannot ship.
    """
    loaded = load_tariffs(path)
    assert loaded.tariffs
    assert loaded.reference in {t.name for t in loaded.tariffs}


@pytest.mark.parametrize("path", SHIPPED_FILES, ids=lambda p: p.name)
def test_edp_indexada_entries_carry_the_sourced_fixed_term(path: Path) -> None:
    """
    An indexada entry on default coefficients IS EDP Indexada Horária.

    Its fixed term is K3 + TAR potência, stored in the file as a number;
    this fails if core's K3 or TAR moves and the files are not updated.
    Entries overriding k1/k2/perdas are another offer and are skipped.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    edp_indexada = [
        entry
        for entry in data["tariff"]
        if entry.get("pricing") == "indexada"
        and not {"k1", "k2", "perdas"} & set(entry)
    ]
    for entry in edp_indexada:
        assert entry["fixed_eur_day"] == pytest.approx(
            INDEXADA_DAILY_FIXED_EUR, abs=5e-5
        ), entry["name"]
    assert pytest.approx(K3 + TAR_POTENCIA_2026) == INDEXADA_DAILY_FIXED_EUR


# --- selecting tariffs (--only) ----------------------------------------


def test_only_narrows_the_comparison_in_file_order(tmp_path: Path) -> None:
    """Selection keeps the file's order, whatever order the names came in."""
    loaded = load_tariffs(
        _write(tmp_path, _VALID), only=["din tri sem", "fixa simples"]
    )
    assert [t.name for t in loaded.tariffs] == ["fixa simples", "din tri sem"]


def test_todos_in_unselected_tariffs_do_not_block_a_run(tmp_path: Path) -> None:
    """
    The point of selecting: run what is filled in while the rest is not.

    A TODO in a tariff left out is not validated at all.
    """
    text = _VALID.replace(
        "prices = { vazio = 0.10, cheias = 0.20, ponta = 0.30 }",
        'prices = { vazio = "TODO", cheias = "TODO", ponta = "TODO" }',
    )
    with pytest.raises(TariffFileError):
        load_tariffs(_write(tmp_path, text))  # the whole file still fails
    loaded = load_tariffs(_write(tmp_path, text), only=["fixa simples", "fixa bi"])
    assert [t.name for t in loaded.tariffs] == ["fixa simples", "fixa bi"]


def test_todos_in_selected_tariffs_still_fail(tmp_path: Path) -> None:
    """Selecting a tariff means comparing it, so its values must be real."""
    text = _VALID.replace("fixed_eur_day = 0.31", 'fixed_eur_day = "TODO"')
    problems = _problems_only(tmp_path, text, ["fixa bi"])
    assert '"fixa bi": fixed_eur_day is TODO' in problems


def test_an_unknown_name_is_an_error_that_lists_the_names(tmp_path: Path) -> None:
    """A typo must not quietly compare nothing — and the fix is one glance away."""
    problems = _problems_only(tmp_path, _VALID, ["fixa simples", "din tri semanal"])
    assert '--only "din tri semanal" matches no tariff' in problems
    assert '"din tri sem"' in problems  # the available names are listed


def test_an_unselected_reference_falls_back_to_the_first_name_given(
    tmp_path: Path,
) -> None:
    """The "vs" column needs a reference; the first selected name stands in."""
    loaded = load_tariffs(
        _write(tmp_path, _VALID), only=["media bi dia", "din tri sem"]
    )
    assert loaded.reference == "media bi dia"
    assert len(loaded.notes) == 1
    assert "fixa simples" in loaded.notes[0]


def test_a_selected_reference_needs_no_note(tmp_path: Path) -> None:
    """Nothing changed, so nothing to report."""
    loaded = load_tariffs(_write(tmp_path, _VALID), only=["fixa simples", "fixa bi"])
    assert loaded.reference == "fixa simples"
    assert loaded.notes == ()


def test_an_unselected_detailed_tariff_skips_the_detailed_report(
    tmp_path: Path,
) -> None:
    """No error: the per-day report is optional, the comparison is not."""
    loaded = load_tariffs(_write(tmp_path, _VALID), only=["fixa simples", "fixa bi"])
    assert loaded.detailed is None


def test_duplicate_selection_names_count_once(tmp_path: Path) -> None:
    """--only "a" "a" is one tariff, not two identical rows."""
    loaded = load_tariffs(
        _write(tmp_path, _VALID), only=["fixa bi", "fixa bi", "fixa simples"]
    )
    assert [t.name for t in loaded.tariffs] == ["fixa simples", "fixa bi"]


def _problems_only(tmp_path: Path, text: str, only: list[str]) -> str:
    with pytest.raises(TariffFileError) as caught:
        load_tariffs(_write(tmp_path, text), only=only)
    return str(caught.value)


def test_file_pricing_names_horaria_media_and_the_old_alias(tmp_path: Path) -> None:
    """`indexada` stays Horária; `indexada_media` is Média with K1 = 1.10."""
    path = tmp_path / "tariffs.toml"
    path.write_text(
        'reference = "old"\n'
        '[[tariff]]\nname = "old"\npricing = "indexada"\noption = "tri"\n'
        'cycle = "semanal"\nfixed_eur_day = 0.3462\n'
        '[[tariff]]\nname = "hor"\npricing = "indexada_horaria"\noption = "simples"\n'
        "fixed_eur_day = 0.3462\n"
        '[[tariff]]\nname = "med"\npricing = "indexada_media"\noption = "bi"\n'
        'cycle = "diario"\nfixed_eur_day = 0.3462\n',
        encoding="utf-8",
    )
    tariffs = {t.name: t for t in load_tariffs(path).tariffs}
    assert tariffs["old"].pricing == "indexada_horaria"
    assert tariffs["hor"].pricing == "indexada_horaria"
    assert tariffs["med"].pricing == "indexada_media"
    assert tariffs["med"].k1 == K1_MEDIA
    assert tariffs["med"].calendars is DAILY_CALENDARS
