"""
The supply contract: how each quarter is priced (ADR-0012).

A tariff has three independent axes:

- **pricing** — `fixa` (a fixed EUR/kWh per period, OMIE ignored),
  `indexada_media` (EDP Indexada Média: the billing window's AVERAGE
  OMIE per period, K1 = 1.10) or `indexada_horaria` (EDP Indexada
  Horária: each quarter at its own OMIE price, K1 = 1.08);
- **option** — `simples`, `bi` or `tri` (horária);
- **cycle** — `semanal` or `diario` (irrelevant for simples).

The calendars emit tri-horária period names, so every option's values
are keyed by those (`core.prices.flat_table` / `bi_table` /
`tri_table`). Indexed tariffs go through `core.prices.price`, the one
delivered-price formula — never re-implemented here.

`DEFAULT_TARIFF` (Indexada Horária, tri-horária, weekly) is what the
integration priced before this parameter existed; it reproduces
`core.prices.price` with its defaults exactly.

Pure: no I/O, no clock reads, no `homeassistant` imports.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .calendar import CYCLE_CALENDARS, Calendars, Cycle, Period, period
from .prices import (
    K1_HORARIA,
    K1_MEDIA,
    K2,
    K3,
    PERDAS,
    TAR_POTENCIA_2026,
    Option,
    TarTable,
    price,
    tar_energia_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date, datetime

Pricing = Literal["fixa", "indexada_media", "indexada_horaria"]

# The groups a price is averaged (Média) or quoted (fixa) over, per
# option: one for simples, vazio / fora de vazio for bi, three for tri.
PriceGroup = Literal["simples", "vazio", "fora_de_vazio", "cheias", "ponta"]

# EDP Indexada's daily fixed term: K3 plus TAR potência at 4.60 kVA
# (docs/tariff-reference.md §1-2). TAR potência does not vary with the
# tariff option, so this is the same for every Indexada option.
INDEXADA_DAILY_FIXED_EUR = K3 + TAR_POTENCIA_2026

_DEFAULT_K1: dict[Pricing, float | None] = {
    "fixa": None,
    "indexada_media": K1_MEDIA,
    "indexada_horaria": K1_HORARIA,
}


@dataclass(frozen=True)
class Tariff:
    """One supply contract: how a quarter is priced, and its daily fixed term."""

    pricing: Pricing
    option: Option
    cycle: Cycle = "semanal"
    # Fixa: the full EUR/kWh per period (required). Indexed: a TAR
    # energia override; None means the ERSE table for the option, looked
    # up by date (core.prices.tar_energia_for).
    energy_by_period: TarTable | None = None
    daily_fixed_eur: float = INDEXADA_DAILY_FIXED_EUR
    # Indexed offers only. None picks the pricing's EDP default.
    k1: float | None = None
    k2: float = K2
    perdas: float = PERDAS
    name: str = ""

    def __post_init__(self) -> None:
        """Fill the pricing's default K1 and reject an unpriceable fixa."""
        if self.pricing == "fixa" and self.energy_by_period is None:
            msg = "a fixa tariff needs its energy_by_period prices"
            raise ValueError(msg)
        if self.k1 is None:
            object.__setattr__(self, "k1", _DEFAULT_K1[self.pricing])
        if not self.name:
            object.__setattr__(self, "name", self.key)

    # --- identity -------------------------------------------------------

    @property
    def key(self) -> str:
        """Stable identifier, e.g. `indexada_horaria/tri/semanal`."""
        cycle = "-" if self.option == "simples" else self.cycle
        return f"{self.pricing}/{self.option}/{cycle}"

    @property
    def indexed(self) -> bool:
        """True when the price follows OMIE (Média or Horária)."""
        return self.pricing != "fixa"

    @property
    def needs_omie(self) -> bool:
        """True when pricing a day needs the OMIE series."""
        return self.indexed

    @property
    def is_flat(self) -> bool:
        """
        True when every quarter of a day costs the same.

        Fixa simples and Média simples: there is no spread to arbitrage,
        so the battery is held as backup (UPS mode, ADR-0011/0012).
        Horária simples is NOT flat — OMIE still moves every quarter.
        """
        return self.option == "simples" and self.pricing != "indexada_horaria"

    # --- periods --------------------------------------------------------

    @property
    def calendars(self) -> Calendars:
        """The period calendar this tariff's cycle uses."""
        return CYCLE_CALENDARS[self.cycle]

    def period_of(self, when: datetime) -> Period:
        """Tri-horária period of an instant under this tariff's cycle."""
        return period(when, self.calendars)

    def price_group(self, name: Period) -> PriceGroup:
        """Return the group a tri-horária period is priced in under this option."""
        if self.option == "simples":
            return "simples"
        if self.option == "bi":
            return "vazio" if name == "vazio" else "fora_de_vazio"
        return name

    def group_of(self, when: datetime) -> PriceGroup:
        """Return the price group of an instant (what a sensor shows as its period)."""
        return self.price_group(self.period_of(when))

    @property
    def discharge_periods(self) -> frozenset[Period]:
        """The periods the static schedule discharges into (empty for simples)."""
        if self.option == "tri":
            return frozenset({"ponta"})
        if self.option == "bi":
            return frozenset({"ponta", "cheias"})
        return frozenset()

    def rates(self, on: date) -> TarTable:
        """
        Per-period rates that RANK the day's periods (no OMIE needed).

        Fixa: the contract price. Indexed: the TAR energia, which
        dominates the delivered spread (CONTEXT.md) — what UPS mode
        ranks by when no prices are available.
        """
        if self.energy_by_period is not None:
            return self.energy_by_period
        return tar_energia_for(on, option=self.option)

    # --- prices ---------------------------------------------------------

    def period_averages(
        self, points: Iterable[tuple[datetime, float]]
    ) -> dict[PriceGroup, float]:
        """
        Mean OMIE (EUR/MWh) per price group over equal-length quarters.

        Indexada Média bills each group at its average over the billing
        window; this is that average over whatever window the caller
        supplies (docs/tariff-reference.md §2, §5).
        """
        sums: dict[PriceGroup, float] = defaultdict(float)
        counts: dict[PriceGroup, int] = defaultdict(int)
        for when, omie in points:
            group = self.group_of(when)
            sums[group] += omie
            counts[group] += 1
        return {group: sums[group] / counts[group] for group in sums}

    def quarter_price(
        self,
        omie_eur_mwh: float | None,
        when: datetime,
        omie_averages: dict[PriceGroup, float] | None = None,
    ) -> float | None:
        """
        EUR/kWh for the quarter starting at `when`, or None if unpriceable.

        A fixed tariff ignores OMIE but still returns None where OMIE is
        missing: in a comparison every tariff must be priced on the SAME
        quarters (backtest/tariffs.py). Production prices fixa days with
        `fixed_price` instead, which needs no OMIE at all.

        Média prices at `omie_averages[group]`; a group missing from the
        averages falls back to this quarter's own OMIE.
        """
        if omie_eur_mwh is None:
            return None
        if self.pricing == "fixa":
            return self.fixed_price(when)
        omie = omie_eur_mwh
        if self.pricing == "indexada_media":
            if omie_averages is None:
                msg = "indexada_media needs the window's omie_averages"
                raise ValueError(msg)
            omie = omie_averages.get(self.group_of(when), omie_eur_mwh)
        return price(
            omie,
            when,
            k1=self.k1,
            k2=self.k2,
            perdas=self.perdas,
            tar_energia=self.rates(when.date()),
            calendars=self.calendars,
        )

    def fixed_price(self, when: datetime) -> float:
        """Return the fixa contract price at an instant."""
        if self.energy_by_period is None:  # pragma: no cover - __post_init__
            msg = "not a fixa tariff"
            raise ValueError(msg)
        return self.energy_by_period[self.period_of(when)]


# The tariff the integration priced before ADR-0012 — and the default
# for any config entry that predates the parameter.
DEFAULT_TARIFF = Tariff(pricing="indexada_horaria", option="tri", cycle="semanal")
