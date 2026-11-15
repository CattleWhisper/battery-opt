"""
Tariff catalogue for the what-if simulations: every option, priced per quarter.

The simulations answer "which contract should I be on", so they need
the SAME consumption priced under every candidate. A tariff here is a
rule turning (OMIE price, instant) into EUR/kWh plus a daily fixed
term — the battery simulation is already generic over a price vector,
so each tariff is just another vector to run it against.

The candidates themselves live in a parameters file
(`backtest/tariffs.toml`, read by `load_tariffs`), so the whole
comparison is one run and every number in it has one visible home.

The tariff model itself — fixa / indexada Média / indexada Horária,
simples / bi / tri, semanal / diário — lives in `core.tariff.Tariff`
(ADR-0012), the same object the integration prices with. This module
adds only the parameters file and two builders:

- **indexada** (EDP Indexada Horária or Média): `core.prices.price`,
  the integration's own delivered-price formula, with the option's
  TAR table, the cycle's calendar and the offer's K1/K2/losses. Média
  prices each group at the billing window's average OMIE
  (`simulate_big_battery.tariff_prices`). Never re-implemented here.
- **fixa**: a fixed EUR/kWh per period, OMIE ignored.

Regulatory values (TAR energia, the calendars) stay in code: they are
the same for every retailer, and docs/tariff-reference.md §1 and §3
are their source. Offer values (fixed prices, fixed terms, K1/K2) are
contract-specific and belong in the parameters file.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from custom_components.battery_opt.core.calendar import (
    CYCLE_CALENDARS,
    DAILY_CALENDARS,
    Cycle,
    Period,
)
from custom_components.battery_opt.core.prices import (
    K2,
    PERDAS,
    TAR_BI_FORA_DE_VAZIO_2026,
    TAR_BI_VAZIO_2026,
    TAR_ENERGIA_2026,
    TAR_SIMPLES_2026,
    Option,
    bi_table,
    flat_table,
    tri_table,
)
from custom_components.battery_opt.core.tariff import (
    INDEXADA_DAILY_FIXED_EUR,
    PriceGroup,
    Pricing,
    Tariff,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CYCLE_CALENDARS",
    "DAILY_CALENDARS",
    "INDEXADA_DAILY_FIXED_EUR",
    "TAR_BI_FORA_DE_VAZIO_2026",
    "TAR_BI_VAZIO_2026",
    "TAR_SIMPLES_2026",
    "Cycle",
    "Option",
    "PriceGroup",
    "Pricing",
    "Tariff",
    "bi_table",
    "dynamic",
    "fixed",
    "flat_table",
    "load_tariffs",
    "tri_table",
]

# The file's `pricing` values. "indexada" is the original spelling and
# stays an alias of Horária, so existing parameter files keep loading.
FilePricing = Literal["fixa", "indexada", "indexada_horaria", "indexada_media"]
_FILE_PRICING: dict[str, Pricing] = {
    "fixa": "fixa",
    "indexada": "indexada_horaria",
    "indexada_horaria": "indexada_horaria",
    "indexada_media": "indexada_media",
}

DEFAULT_TARIFFS_FILE = Path(__file__).with_name("tariffs.toml")

_TAR_BY_OPTION: dict[Option, dict[Period, float]] = {
    "simples": flat_table(TAR_SIMPLES_2026),
    "bi": bi_table(TAR_BI_VAZIO_2026, TAR_BI_FORA_DE_VAZIO_2026),
    "tri": dict(TAR_ENERGIA_2026),
}


def dynamic(  # noqa: PLR0913 - the offer's coefficients are keyword-only
    option: Option,
    cycle: Cycle,
    daily_fixed_eur: float,
    *,
    name: str | None = None,
    pricing: Pricing = "indexada_horaria",
    k1: float | None = None,
    k2: float = K2,
    perdas: float = PERDAS,
) -> Tariff:
    """
    Build an indexed offer: OMIE through the delivered-price formula, plus TAR.

    `k1` defaults to the pricing's EDP value (Horária 1.08, Média 1.10).
    """
    label = "indexada" if pricing == "indexada_horaria" else "indexada media"
    return Tariff(
        pricing=pricing,
        option=option,
        cycle=cycle,
        energy_by_period=dict(_TAR_BY_OPTION[option]),
        daily_fixed_eur=daily_fixed_eur,
        k1=k1,
        k2=k2,
        perdas=perdas,
        name=name or f"{label} {option} {cycle}",
    )


def fixed(
    option: Option,
    energy_by_period: dict[Period, float],
    daily_fixed_eur: float,
    cycle: Cycle = "semanal",
    *,
    name: str | None = None,
) -> Tariff:
    """
    Build a fixed-price offer. Simples has no cycle; its calendar is irrelevant.

    Prices must come from the actual offer — they are contract- and
    discount-specific, so nothing here supplies a default.
    """
    return Tariff(
        pricing="fixa",
        option=option,
        cycle=cycle,
        energy_by_period=energy_by_period,
        daily_fixed_eur=daily_fixed_eur,
        name=name or f"fixa {option} {cycle}",
    )


# --- the parameters file ---------------------------------------------

TODO = "TODO"

# The price keys a fixed offer must give, per option — exactly these.
PRICE_KEYS: dict[Option, tuple[str, ...]] = {
    "simples": ("simples",),
    "bi": ("vazio", "fora_de_vazio"),
    "tri": ("vazio", "cheias", "ponta"),
}
_COEFFICIENTS = ("k1", "k2", "perdas")
_ENTRY_KEYS = frozenset(
    {"name", "pricing", "option", "cycle", "fixed_eur_day", "prices", *_COEFFICIENTS}
)
_TOP_KEYS = frozenset({"reference", "detailed", "tariff"})


class TariffFileError(ValueError):
    """The parameters file is incomplete or malformed; lists every problem."""


@dataclass(frozen=True)
class TariffFile:
    """A loaded parameters file."""

    tariffs: list[Tariff]  # in file order, selection applied
    reference: str  # name of the tariff the "vs" column compares against
    detailed: str | None  # name of the tariff given the per-day report
    # Anything the selection changed that the reader should be told.
    notes: tuple[str, ...] = ()


def _number(value: Any, where: str, problems: list[str]) -> float | None:
    """Accept a non-negative number; record why anything else is not one."""
    if value == TODO:
        problems.append(f"{where} is TODO")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"{where} must be a number, got {value!r}")
        return None
    if value < 0:
        problems.append(f"{where} must not be negative, got {value}")
        return None
    return float(value)


def _choice(value: Any, allowed: Any, where: str, problems: list[str]) -> Any:
    """Accept one of a Literal's values; record anything else."""
    options = get_args(allowed)
    if value not in options:
        problems.append(f"{where} must be one of {', '.join(options)}; got {value!r}")
        return None
    return value


def _fixed_prices(
    entry: dict[str, Any], option: Option, where: str, problems: list[str]
) -> dict[Period, float] | None:
    """Read a fixed offer's `prices` table: exactly the option's keys."""
    keys = PRICE_KEYS[option]
    raw = entry.get("prices")
    if not isinstance(raw, dict):
        problems.append(f"{where}: pricing fixa needs prices = {{ {', '.join(keys)} }}")
        return None
    if set(raw) != set(keys):
        got = ", ".join(sorted(raw)) or "none"
        problems.append(
            f"{where}: prices for option {option} must be exactly "
            f"{', '.join(keys)}; got {got}"
        )
        return None
    values = {
        key: _number(raw[key], f"{where}: prices.{key}", problems) for key in keys
    }
    if any(value is None for value in values.values()):
        return None
    if option == "simples":
        return flat_table(values["simples"])
    if option == "bi":
        return bi_table(values["vazio"], values["fora_de_vazio"])
    return tri_table(values["vazio"], values["cheias"], values["ponta"])


def _entry(entry: Any, index: int, problems: list[str]) -> Tariff | None:
    """Parse one [[tariff]]; every problem is recorded, not just the first."""
    if not isinstance(entry, dict):
        problems.append(f"tariff #{index} is not a table")
        return None
    name = entry.get("name")
    has_name = isinstance(name, str) and bool(name.strip())
    where = f'tariff #{index} "{name}"' if has_name else f"tariff #{index}"
    before = len(problems)
    if not has_name:
        problems.append(f"{where}: name is required")
    problems.extend(
        f"{where}: unknown key {key!r}" for key in sorted(set(entry) - _ENTRY_KEYS)
    )
    pricing = _choice(entry.get("pricing"), FilePricing, f"{where}: pricing", problems)
    option = _choice(entry.get("option"), Option, f"{where}: option", problems)
    # Simples has no periods, so its cycle is irrelevant and optional.
    cycle = "semanal"
    if option != "simples" or "cycle" in entry:
        cycle = _choice(entry.get("cycle"), Cycle, f"{where}: cycle", problems)
    fixed_eur_day = _number(
        entry.get("fixed_eur_day"), f"{where}: fixed_eur_day", problems
    )

    prices = None
    coefficients: dict[str, float | None] = {}
    if pricing == "fixa":
        if option is not None:
            prices = _fixed_prices(entry, option, where, problems)
        problems.extend(
            f"{where}: {key} only applies to pricing indexada"
            for key in _COEFFICIENTS
            if key in entry
        )
    elif pricing is not None:
        if "prices" in entry:
            problems.append(f"{where}: indexada takes no prices (OMIE + TAR set them)")
        coefficients = {
            key: _number(entry[key], f"{where}: {key}", problems)
            for key in _COEFFICIENTS
            if key in entry
        }

    if len(problems) > before:
        return None
    if pricing == "fixa":
        return fixed(option, prices, fixed_eur_day, cycle, name=name)
    return dynamic(
        option,
        cycle,
        fixed_eur_day,
        name=name,
        pricing=_FILE_PRICING[pricing],
        **coefficients,
    )


def load_tariffs(
    path: Path = DEFAULT_TARIFFS_FILE, only: Sequence[str] | None = None
) -> TariffFile:
    """
    Read and validate a parameters file, or raise listing EVERY problem.

    All-or-nothing on purpose: the point of the file is comparing every
    tariff at once, and a run that quietly dropped the one with a
    missing fixed term would rank the rest as if it did not exist.

    `only` narrows the comparison to the named tariffs. It is applied
    BEFORE the values are validated, so an entry left out may still
    hold TODOs — that is what lets a partly filled file run. The file
    as a whole is still checked (duplicate names, the reference and
    detailed names), and a name that matches no tariff is an error, not
    an empty column. If the file's reference is not selected, the first
    name in `only` stands in for it and a note says so; an unselected
    `detailed` just skips the per-day report.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        msg = f"{path}: {err}"
        raise TariffFileError(msg) from err

    problems = [
        f"unknown top-level key {key!r}" for key in sorted(set(data) - _TOP_KEYS)
    ]
    entries = data.get("tariff")
    if not isinstance(entries, list) or not entries:
        problems.append("no [[tariff]] entries")
        entries = []
    names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
    problems.extend(
        f'tariff name "{name}" is used {count} times'
        for name, count in Counter(names).items()
        if count > 1
    )
    reference = data.get("reference")
    if reference not in names:
        problems.append(f"reference {reference!r} must name one of the tariffs")
    detailed = data.get("detailed")
    if detailed is not None and detailed not in names:
        problems.append(f"detailed {detailed!r} must name one of the tariffs")

    selected = list(dict.fromkeys(only)) if only is not None else None
    if selected is not None:
        available = ", ".join(f'"{name}"' for name in names if isinstance(name, str))
        problems.extend(
            f'--only "{name}" matches no tariff; available: {available}'
            for name in selected
            if name not in names
        )
    tariffs = [
        tariff
        for index, entry in enumerate(entries, start=1)
        if selected is None
        or (isinstance(entry, dict) and entry.get("name") in selected)
        if (tariff := _entry(entry, index, problems)) is not None
    ]

    if problems:
        listing = "\n".join(f"  - {problem}" for problem in problems)
        msg = f"{path}: {len(problems)} problem(s) to fix:\n{listing}"
        raise TariffFileError(msg)

    notes: list[str] = []
    if selected is not None:
        if reference not in selected:
            notes.append(
                f'reference "{reference}" is not selected; '
                f'comparing against "{selected[0]}" instead'
            )
            reference = selected[0]
        if detailed not in selected:
            detailed = None
    return TariffFile(
        tariffs=tariffs, reference=reference, detailed=detailed, notes=tuple(notes)
    )
