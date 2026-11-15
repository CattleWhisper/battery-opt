# ADR-0012: The supply contract is a parameter — pricing × option × cycle

**Status:** Accepted (owner, 2026-11-15) · **Date:** 2026-11
**Relates to:** ADR-0005 (versioned calendar; extended to the daily
cycle and per-option TAR), ADR-0011 (UPS mode; amended), ADR-0001
(HA-free core)

## Context

Until now the integration priced exactly one contract: **EDP Indexada
Horária, tri-horária, weekly cycle**. `prices_source` called
`core.prices.price(omie, start)` with its defaults, and the weekly
tri-horária calendar was implicit in the static schedule, the UPS
ranking, the price-segment labels, the current-price sensor and the
cost sensor's fixed term. There was no configuration key for it at all.

The owner's actual contract is fixed-price simples (CONTEXT.md §Current
tariff state), and the choice of contract is exactly what the
`backtest/tariffs.py` comparison exists to inform. That module already
modelled fixa/indexada × simples/bi/tri × semanal/diário — but only for
the what-if simulations; the Média (monthly-average) mechanism lived
separately in `backtest/run.py`.

The owner asked for one parameter with three axes, and for the
calculations to follow it:

- **Pricing:** fixa (pre-set price) · indexada média (the billing
  window's average OMIE) · indexada quarto-horária (dynamic, per 15 min)
- **Option:** simples · bi-horária · tri-horária
- **Cycle:** diário · semanal

and, specifically, that **simples on a flat price means UPS mode** —
there is no period to discharge into, so the battery's only job is to
stay charged — while the UPS switch stays available on every other
tariff.

## Decision

1. **One model, in core.** `core/tariff.py` holds `Tariff(pricing,
   option, cycle, energy_by_period, daily_fixed_eur, k1, k2, perdas)`.
   The daily-cycle calendar (`DAILY_CALENDARS`, `CYCLE_CALENDARS`) moves
   into `core/calendar.py` and the simples / bi-horária TAR into
   `core/prices.py` (`TAR_TABLES_BY_OPTION`), both versioned by
   effective date like their tri-horária counterparts (ADR-0005). The
   backtest imports this model instead of owning one.
2. **Indexed tariffs go through `core.prices.price`** — never
   re-implemented. Horária prices each quarter at its own OMIE; Média
   prices each *price group* at its average OMIE, where the groups are
   the option's (one for simples; vazio / fora de vazio for bi; the
   three periods for tri). K1 defaults per pricing: 1.08 Horária, 1.10
   Média (EDP sheets, docs/tariff-reference.md §2).
3. **Média plans against an estimate.** The real average is only known
   at the end of the billing window, so production uses the archived
   OMIE of the trailing 30 days plus the day itself, and flags the
   series `prices_estimated`. The backtest uses the true EDP window
   [day 2 of M, day 2 of M+1).
4. **Fixa needs no OMIE.** The day is built from the contract prices
   (`prices_source.fixed_day_series`) — always priceable, never padded
   — and the config flow stops requiring the OMIE integration.
5. **A flat tariff forces UPS; the switch is untouched.**
   `Tariff.is_flat` is true for fixa simples and Média simples (every
   quarter of the day costs the same). The coordinator's `ups_active =
   ups_mode or tariff.is_flat` selects the UPS builder and is what the
   executor reads. `switch.battery_opt_ups_mode` keeps its own state and
   keeps working on every periodic tariff; on a flat tariff it is
   redundant, and its state survives a later tariff change. The plan
   sensor reports `ups_reason: switch | tariff`. **Horária simples is
   not flat** — OMIE still moves every quarter — so it keeps the
   optimiser.
6. **The static schedule generalises, tri-horária weekly unchanged.**
   It discharges into `tariff.discharge_periods` (tri: ponta; bi: all of
   fora de vazio; simples: none); a day with no discharge interval is a
   no-op day (the weekly Sat/Sun rule, now derived); the overnight
   window is the vazio before the day's first discharge (00:00–07:00
   weekly, 00:00–08:00 daily); bi-horária always charges overnight,
   since its midday is fora de vazio. Day chaining skips no-op days the
   same way. Intervals are elapsed time from midnight
   (`calendar.interval_starts`), so under the daily cycle a DST Sunday
   keeps its true wall-clock periods. A test pins the default tariff to
   the pre-ADR plan for every day of two years.
7. **Configuration.** Entry v3: three selectors on the main form
   (defaults = the pre-ADR contract), and a `tariff_prices` step for
   fixa only (its option's prices + the daily fixed term). The v2→v3
   migration writes Indexada Horária / tri / semanal into every
   existing entry, so nothing changes until the owner picks otherwise.
   `tariff_config.tariff_from_config` is the single translation point.
8. **Downstream.** The cost sensor's fixed term is
   `tariff.daily_fixed_eur`; `tar_period` and the price segments show
   the contract's own groups; the price archive records the tariff
   key, and the best-periods history pools only days priced under the
   active one (files without a key predate this ADR and count as the
   default tariff).

## Consequences

- **Checkpoint C is unaffected.** The default tariff reproduces the
  previous prices bit for bit (week-long test, naive and aware
  instants) and the previous static plan for every day of 2025–2026;
  the backtest's existing tariff rows are unchanged to the cent.
- **Verification debt: the daily cycle.** Its spans come from a
  secondary source and have not been read against the ERSE order
  (docs/tariff-reference.md §3). The integration can now *actuate* on
  it, so this must be checked before running a battery on a
  daily-cycle contract.
- **Média is estimated.** The plan's ranking is right whenever the
  per-period order of the average is stable, which the TAR spread
  makes near-certain for bi/tri; the reported euro figures are
  estimates until the window closes.
- **Horária simples under dry-run idles.** Simples has no discharge
  period, so the static schedule is empty; arbitraging OMIE needs
  `dry_run` off (the greedy). This is the honest static baseline for a
  contract with no periods, not a bug.
- **Archive comparability.** Switching tariff starts a fresh
  best-periods history (other-tariff days are skipped), and a fixa day
  archives no OMIE — a later switch to Média estimates from fewer days.

## Alternatives rejected

**A fourth "dry-run-style" regime per tariff.** Rejected: the tariff is
a fact about the contract, not a control regime; it belongs in config,
and dry-run stays orthogonal to it.

**Turn the UPS switch on automatically for a flat tariff.** Rejected on
the owner's instruction (2026-11-15) that the switch must remain the
owner's choice on the other tariffs: writing it from the tariff would
lose the owner's state when the tariff changes back. A flat tariff adds
a reason for UPS; it never overrides the switch.

**A separate config step for the tariff axes.** Rejected for the main
form: three selectors with sensible defaults fit beside the existing
parameters, and only fixa genuinely needs more input (its prices),
which gets its own step.

**A catalogue of retailer offers** (e.g. the EDP sheets in
`backtest/tariffs-edp-2026.toml`) instead of typed prices. Rejected:
offers change and are retailer-specific; the typed fields work for any
supplier. The catalogue stays a backtest input.
