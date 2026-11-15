"""
Config entry -> core `Tariff` (ADR-0012): the one translation point.

Kept free of homeassistant imports so it is testable like `core/`. An
entry that predates the parameter has none of these keys and gets
`DEFAULT_TARIFF` (Indexada Horária, tri-horária, weekly) — the
contract the integration priced before ADR-0012.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    CONF_FIXED_EUR_DAY,
    CONF_PRICE_CHEIAS,
    CONF_PRICE_FORA_DE_VAZIO,
    CONF_PRICE_PONTA,
    CONF_PRICE_SIMPLES,
    CONF_PRICE_VAZIO,
    CONF_TARIFF_CYCLE,
    CONF_TARIFF_OPTION,
    CONF_TARIFF_PRICING,
    DEFAULT_TARIFF_CYCLE,
    DEFAULT_TARIFF_OPTION,
    DEFAULT_TARIFF_PRICING,
    FIXA_PRICE_KEYS,
)
from .core.prices import bi_table, flat_table, tri_table
from .core.tariff import INDEXADA_DAILY_FIXED_EUR, Tariff

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .core.prices import TarTable

PRICINGS = ("fixa", "indexada_media", "indexada_horaria")
OPTIONS = ("simples", "bi", "tri")
CYCLES = ("semanal", "diario")


class TariffConfigError(ValueError):
    """The stored tariff configuration cannot be priced."""


def tariff_axes(merged: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return (pricing, option, cycle), defaulted and checked."""
    pricing = merged.get(CONF_TARIFF_PRICING, DEFAULT_TARIFF_PRICING)
    option = merged.get(CONF_TARIFF_OPTION, DEFAULT_TARIFF_OPTION)
    cycle = merged.get(CONF_TARIFF_CYCLE, DEFAULT_TARIFF_CYCLE)
    if pricing not in PRICINGS or option not in OPTIONS or cycle not in CYCLES:
        msg = f"unknown tariff {pricing}/{option}/{cycle}"
        raise TariffConfigError(msg)
    return pricing, option, cycle


def missing_fixa_prices(merged: Mapping[str, Any]) -> list[str]:
    """Return the fixa price keys the option needs that are not set."""
    _pricing, option, _cycle = tariff_axes(merged)
    keys = (*FIXA_PRICE_KEYS[option], CONF_FIXED_EUR_DAY)
    return [key for key in keys if merged.get(key) is None]


def _fixa_table(option: str, merged: Mapping[str, Any]) -> TarTable:
    if option == "simples":
        return flat_table(float(merged[CONF_PRICE_SIMPLES]))
    if option == "bi":
        return bi_table(
            float(merged[CONF_PRICE_VAZIO]), float(merged[CONF_PRICE_FORA_DE_VAZIO])
        )
    return tri_table(
        float(merged[CONF_PRICE_VAZIO]),
        float(merged[CONF_PRICE_CHEIAS]),
        float(merged[CONF_PRICE_PONTA]),
    )


def tariff_from_config(merged: Mapping[str, Any]) -> Tariff:
    """
    Build the entry's `Tariff` from its effective config (data + options).

    Raises `TariffConfigError` for a fixa contract missing a price —
    the config flow refuses to save one, so this only fires on a
    hand-edited entry, and refusing beats pricing it at a guess.
    """
    pricing, option, cycle = tariff_axes(merged)
    if option == "simples":
        cycle = "semanal"  # irrelevant: one price group all day
    if pricing != "fixa":
        return Tariff(
            pricing=pricing,  # type: ignore[arg-type]
            option=option,  # type: ignore[arg-type]
            cycle=cycle,  # type: ignore[arg-type]
            daily_fixed_eur=INDEXADA_DAILY_FIXED_EUR,
        )
    missing = missing_fixa_prices(merged)
    if missing:
        msg = f"fixa {option} tariff is missing {', '.join(missing)}"
        raise TariffConfigError(msg)
    return Tariff(
        pricing="fixa",
        option=option,  # type: ignore[arg-type]
        cycle=cycle,  # type: ignore[arg-type]
        energy_by_period=_fixa_table(option, merged),
        daily_fixed_eur=float(merged[CONF_FIXED_EUR_DAY]),
    )
