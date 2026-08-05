"""
EDP Indexada price model (docs/tariff-reference.md §2).

    price = OMIE/1000 * (1 + PERDAS) * K1 + K2 + TAR_energia(period)

K1/K2/PERDAS are injectable so Horária (K1=1.08) and Média (K1=1.10)
can be compared; defaults are Horária. K3, TAR potência and VAT are
uniform constants that cannot change the optimum — they enter
`total_daily_cost` (reporting) only, never the optimisation (spec §4).

Every constant here comes from CONTEXT.md / docs/tariff-reference.md.
Do not invent or "correct" values: they are verifiable against the
ERSE 2026 tariff order and the EDP standardised offer sheets.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from .calendar import CALENDARS, Calendars, Period, period

if TYPE_CHECKING:
    from datetime import datetime

# --- EDP Eletricidade Indexada DD+FE, effective 01/01/2026 ---
K1_HORARIA = 1.08
K1_MEDIA = 1.10
K2 = 0.0185  # EUR/kWh
K3 = 0.1171  # EUR/day — fixed term, reporting only
PERDAS = 0.164  # flat in v1; Horária actually varies per quarter-hour

# --- TAR energia, ERSE 2026, BTN <=20.7 kVA, tri-horária (EUR/kWh) ---
TAR_ENERGIA_2026: dict[Period, float] = {
    "ponta": 0.2452,
    "cheias": 0.0412,
    "vazio": 0.0158,
}
TAR_POTENCIA_2026 = 0.2291  # EUR/day @ 4.6 kVA — constant, reporting only
VAT = 1.23  # uniform multiplier — reporting only

TarTable = dict[Period, float]

# TAR values are versioned by effective date on an axis INDEPENDENT of
# the hour-span calendar (see ADR-0005 consequences): ERSE revises the
# values annually (+3.5% BTN 2025->2026) on a different cadence from
# period-hour reforms. Adding a 2027 row here must never touch
# calendar.CALENDARS, and vice versa.
TAR_TABLES: tuple[tuple[date, TarTable], ...] = ((date(2026, 1, 1), TAR_ENERGIA_2026),)

# "forward": apply the CURRENT (latest) TAR table to every date. The
# backtest estimates forward economics under the tariff we are moving
# to — it is not a reconstruction of historical bills. "historical"
# (per-date lookup) exists for reconciliation work but is not the
# default.
TAR_POLICY = "forward"


def tar_energia_for(
    on: date,
    *,
    tables: tuple[tuple[date, TarTable], ...] = TAR_TABLES,
    policy: str = TAR_POLICY,
) -> TarTable:
    """Return the TAR energia table to apply on a date, per policy."""
    if policy == "forward":
        return tables[-1][1]
    if policy == "historical":
        chosen: TarTable | None = None
        for effective, table in tables:
            if effective <= on:
                chosen = table
        if chosen is None:
            msg = f"No TAR table in force on {on}; earliest is {tables[0][0]}"
            raise ValueError(msg)
        return chosen
    msg = f"Unknown TAR policy: {policy!r}"
    raise ValueError(msg)


# Injectable K1/K2/PERDAS/TAR are the Task 2 spec (Horária vs Média
# comparison at Checkpoint B), so the argument count is intentional.
def price(  # noqa: PLR0913
    omie_eur_mwh: float,
    dt: datetime,
    *,
    k1: float = K1_HORARIA,
    k2: float = K2,
    perdas: float = PERDAS,
    tar_energia: dict[Period, float] | None = None,
    calendars: Calendars = CALENDARS,
) -> float:
    """Energy price in EUR/kWh for an instant, excluding fixed terms and VAT."""
    tar = tar_energia_for(dt.date()) if tar_energia is None else tar_energia
    return omie_eur_mwh / 1000 * (1 + perdas) * k1 + k2 + tar[period(dt, calendars)]


def total_daily_cost(
    energy_cost_eur: float,
    *,
    days: float = 1.0,
    k3: float = K3,
    tar_potencia: float = TAR_POTENCIA_2026,
    vat: float = VAT,
) -> float:
    """
    Total billed cost including fixed terms and VAT — reporting only.

    `energy_cost_eur` is the sum of price() * consumption over the
    window. The added terms are uniform and never change the optimum.
    """
    return (energy_cost_eur + (k3 + tar_potencia) * days) * vat
