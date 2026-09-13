"""
UPS mode: buy the day's cheapest energy, never sell it back (ADR-0011).

The battery stops being an arbitrage asset and becomes a reserve. The
plan therefore has exactly one job — get the pack full, as cheaply as
the day allows — and one prohibition: `discharge_w` is all zeros, in
every interval, unconditionally. Nothing here can command a discharge,
which is what makes the mode auditable rather than merely configured.

Two properties are worth stating because the rest of the module is
built to preserve them:

- **The charge window is seed-independent.** Its size comes from the
  gap between the reserve floor and usable capacity — a completely
  drained pack — never from where the model thinks the battery is.
  A seed that is too high therefore shrinks the modelled energy but
  not the window, so the CHARGE state still spans every cheap
  interval and the run-time pair (charge loop + the firmware's
  percent target) tops up whatever the model did not know about.
  Without this a UPS whose modelled SoC drifted above reality would
  quietly stop topping up and stay there — the mode's worst failure,
  since the shortfall is invisible until the grid actually fails.
- **Arming needs no donors.** Intervals the model leaves empty inside
  the window carry `ARMED_CHARGE_KWH` (core.plan): a state selector
  per ADR-0007, not an energy claim. `static_schedule` funds its
  arming by shaving already-allocated intervals, which cannot work
  here — the commonest UPS day allocates nothing at all because the
  model believes the pack is full. So `ARMING_HEADROOM_KWH` is held
  back from usable capacity instead and the seed is clamped to fit
  under it (`ups_start_soc_kwh`), leaving room for the armed total by
  construction. Without the clamp the steady-state UPS day — model
  says 100%, every window interval armed — would add energy on top of
  a full pack and fail C-5, which the executor fails closed on: the
  mode would stop actuating precisely once it had succeeded.

Prices are the delivered EUR/kWh vector when the caller has one. With
none (a publication gap, or the executor's own offline fallback) the
TAR energia table ranks the intervals instead: vazio < cheias <
ponta. That is not a price — it is the only ordering that is always
available offline, it is the component this project arbitrages
anyway, and it keeps a priceless day from filling the pack during
ponta.

Pure module: no I/O, no clock reads, no Home Assistant imports
(ADR-0001).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .calendar import CALENDARS, Calendars, period
from .plan import ARMED_CHARGE_KWH, BatteryParams, Plan
from .prices import tar_energia_for

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date


# Modelling headroom held back from usable capacity so every interval
# of a charge window can be armed without the SoC trajectory crossing
# C-5. 1e-3 kWh covers 100 armed intervals — a DST-long day, the most
# a 15-minute plan can hold — and is 1 Wh, three orders of magnitude
# below anything the pack can resolve.
ARMING_HEADROOM_KWH = 1e-3


def ups_start_soc_kwh(
    measured_kwh: float | None,
    params: BatteryParams,
) -> float:
    """
    Return the UPS seed (ADR-0011): a measurement, else the reserve floor.

    Not the chained static end and not the greedy chain — both model a
    day of ponta discharging, which this mode does not do, so both
    drift further from reality the longer it stays on. Seeding at the
    floor when nothing was measured can only over-book CHARGE, never a
    discharge (`ups_plan` cannot emit one), and the window is sized
    from the floor regardless of the seed: the seed moves the
    published trajectory and the forecast cost, not the battery.

    Clamped into `[cap_min, cap_usable - ARMING_HEADROOM_KWH]`. The
    upper clamp is what keeps a genuinely full pack armable; the floor
    wins outright if a battery is ever configured with less usable
    range than the headroom, and `ups_plan` then simply plans no
    charging at all.
    """
    raw = params.cap_min_kwh if measured_kwh is None else measured_kwh
    ceiling = params.cap_usable_kwh - ARMING_HEADROOM_KWH
    return max(params.cap_min_kwh, min(raw, ceiling))


def tar_rank(
    day: date,
    n: int,
    params: BatteryParams,
    calendars: Calendars = CALENDARS,
) -> list[float]:
    """
    Per-interval TAR energia (EUR/kWh) — a RANKING proxy, not a price.

    Deliberately not the delivered price: it omits the OMIE
    commodity, K1/K2 and the losses, so it must never reach a cost
    calculation. Its only use is ordering intervals cheapest-first
    when no real price vector exists, and for that it is exact — the
    TAR spread (0.0158 vazio to 0.2452 ponta) dominates the intraday
    commodity spread, which is why this project arbitrages TAR in the
    first place.
    """
    tar = tar_energia_for(day)
    # Naive = Portugal legal time, the calendar API contract.
    midnight = datetime(day.year, day.month, day.day)  # noqa: DTZ001
    return [
        tar[period(midnight + timedelta(hours=i * params.interval_hours), calendars)]
        for i in range(n)
    ]


def ups_plan(
    day: date,
    load_w: Sequence[float],
    params: BatteryParams,
    prices_eur_kwh: Sequence[float] | None = None,
    calendars: Calendars = CALENDARS,
) -> Plan:
    """
    Build one day's UPS plan: charge cheapest-first, never discharge.

    Takes no `solar_w`, unlike the other plan builders: a UPS plan has
    no discharge to net against it, and C-3 charge headroom is set by
    GROSS house load because solar cannot raise the contracted
    ceiling. There is nothing for it to change.

    With `prices_eur_kwh` None the TAR table ranks the intervals
    (`tar_rank`). Ties — which are the norm under TAR ranking, where
    a whole vazio stretch shares one value — break toward the earlier
    interval, so the reserve is restored as early as the cheap band
    allows rather than at the far end of it.
    """
    n = len(load_w)
    dt = params.interval_hours
    eta = params.eta_one_way
    charge = [0.0] * n
    prices = (
        list(prices_eur_kwh)
        if prices_eur_kwh is not None
        else tar_rank(day, n, params, calendars)
    )

    # Grid-side charge capacity per interval (C-3). Gross load, not
    # net: the contracted-power ceiling is what limits charging.
    capacity_kwh = [
        max(0.0, min(params.p_charge_max_w, params.p_usable_w - load_w[i])) * dt / 1000
        for i in range(n)
    ]
    order = sorted(range(n), key=lambda i: (prices[i], i))

    # The window: the cheapest intervals that could refill a pack
    # sitting on the reserve floor. Sized from the floor, NOT from
    # this day's seed — see the module docstring.
    refill_kwh = (params.cap_usable_kwh - params.cap_min_kwh) / eta
    window: list[int] = []
    booked = 0.0
    for i in order:
        if booked >= refill_kwh:
            break
        if capacity_kwh[i] <= ARMED_CHARGE_KWH:
            continue  # no room to charge here, and none to arm either
        window.append(i)
        booked += capacity_kwh[i]

    # Fill to just under capacity, leaving the arming headroom free.
    # Seeds above it are the caller's to clamp (`ups_start_soc_kwh`);
    # a seed that slips through simply books no energy, and the window
    # is armed either way.
    target_kwh = params.cap_usable_kwh - ARMING_HEADROOM_KWH
    deficit_kwh = max(0.0, target_kwh - params.start_soc_kwh) / eta
    for i in window:  # already cheapest-first
        take = min(capacity_kwh[i], deficit_kwh)
        deficit_kwh -= take
        charge[i] = max(take, ARMED_CHARGE_KWH) / dt * 1000

    return Plan(charge_w=tuple(charge), discharge_w=(0.0,) * n)
