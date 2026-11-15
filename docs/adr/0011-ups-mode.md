# ADR-0011: UPS mode — charge on the cheapest energy, never discharge

**Status:** Accepted (owner, 2026-09-13); amended by ADR-0012 · **Date:** 2026-09
**Relates to:** ADR-0006 (the three states it uses), ADR-0007 (armed
charge windows), ADR-0010 (the seed that keeps it honest)

## Context

The project treats the battery as an arbitrage asset: every kWh it
holds is there to be sold back into ponta. The owner wants to be able
to suspend that and hold the pack as backup instead — "it will charge
during the cheap periods but not forcefully discharge, will only keep
the backup power in case of grid failure".

Nothing in the integration expressed this. The nearest existing
control is `switch.battery_opt_executor_actuation`, which stops all
driver writes — that leaves the battery wherever it was, under
whatever mode was last written, and stops it charging too. It is a
maintenance gate, not a mode.

Three constraints shaped the answer:

1. **The static schedule discharges.** So does the greedy. Every
   fallback path in the executor lands on one of them, which means
   "never discharge" cannot be implemented as a filter over an
   existing plan — every path has to be considered, including the
   failure paths.
2. **Suppressing discharge in a plan built for arbitrage breaks it.**
   The summer static schedule charges at midday, AFTER the morning
   ponta, and sizes that charge against a pack the ponta has drained.
   Zero out the discharge and the midday fill overshoots the ceiling:
   a C-5 violation, which the executor fails closed on. The mode needs
   its own builder, not a post-processing step.
3. **A UPS that is not full is not a UPS.** The failure that matters
   is the pack sitting at 80% while the model believes it is at 100%,
   because nothing then tops it up and the shortfall is invisible
   until the grid actually fails.

## Decision

**A new plan builder, `core/ups.py`, and a switch that selects it.**
While `switch.battery_opt_ups_mode` is on, every plan the integration
builds charges on the day's cheapest energy and emits
`discharge_w = 0` in every interval.

- **`discharge_w` is structurally zero**, not filtered to zero. The
  builder has no code path that writes a discharge. The mode is
  therefore auditable by reading one function rather than by tracing
  which plan reached the driver.
- **The charge window is sized from the reserve floor, not from the
  seed.** It is the cheapest set of intervals that could refill a
  completely drained pack. A seed that is too high shrinks the
  modelled energy but not the window, so the CHARGE state still spans
  the whole cheap band and the run-time pair — the ADR-0007 charge
  loop plus the firmware's percent target — tops up whatever the
  model did not know about. This is what answers constraint 3.
- **Intervals the model leaves empty inside that window are armed**
  at `ARMED_CHARGE_KWH`, the ADR-0007 state selector. Unlike
  `static_schedule`, the arming is not funded by shaving allocated
  intervals — the commonest UPS day allocates nothing, because the
  model believes the pack is full. `ARMING_HEADROOM_KWH` (1 Wh) is
  held back from usable capacity instead, and the seed is clamped
  under it. Without that clamp the steady state of the mode — model
  at 100%, every window interval armed — adds energy on top of a full
  pack and fails C-5, so the mode would stop actuating precisely once
  it had succeeded.
- **The seed is the ADR-0010 measurement, else the reserve floor.**
  Not the static chain and not the greedy chain: both model a day of
  ponta discharging, which this mode does not do, so both drift
  further from reality the longer it stays on. Seeding at the floor
  can only over-book CHARGE, never a discharge.
- **With no prices, TAR ranks the intervals** (vazio < cheias <
  ponta). Not a price and never used as one — it is the only ordering
  always available offline, and it stops a publication gap from
  filling the reserve at 0.2452 EUR/kWh.
- **The mode overrides dry-run.** Dry-run decides *which arbitrage
  plan* actuates; both of its options discharge, so it cannot survive.
- **Failure paths stay inside the mode.** A published UPS plan that
  somehow fails validation demotes to the executor's own offline UPS
  plan, never to the static schedule — the demotion must not do what
  the mode exists to prevent.
- **The switch is the single source of truth** (`coordinator.ups_mode`),
  read live by the executor on every tick. Published plans carry a
  `ups` flag and the executor adopts one only when it matches: that
  closes the window between flipping the switch and the coordinator's
  next refresh, in which `executor_plan` still holds an arbitrage plan
  that would discharge.

Flipping the switch re-solves the day and applies the result
immediately, rather than at the next quarter boundary. A storm
warning is the reason the control exists, and "it will stop
discharging within 15 minutes" is the wrong answer to one.

## What this does NOT do

**It does not make the grid-failure switchover work.** That is the
battery's own EPS/UPS function, in firmware, on its own output
circuit. This integration plans and commands states over RS485; when
the grid fails, the link and the planner are irrelevant. What the
mode guarantees is narrower and is the part the integration actually
controls: *the stored energy will still be there.* Whether the house
is wired to receive it is an installation question, unverified here.

It also does not detect grid failure, change the reserve floor
(ADR-0008: the battery's), or alter the contracted-power ceiling.

## Consequences

- **It costs money, and says so.** A UPS plan only buys, so its
  `forecast_saving_eur` is negative — the standing cost of holding
  the reserve — and `vs_static_eur` prices the arbitrage being given
  up. The static baseline keeps being published beside the UPS plan
  so the comparison is visible rather than inferred. Reported as-is:
  the owner asked for backup, not for a flattering number.
- **Holding at 100% is harder on the cells** than cycling around the
  middle. Accepted deliberately and for the duration the switch is
  on; it is not a default and not intended as a permanent regime.
- **The greedy chain is not advanced by a UPS day.** That record
  tracks the arbitrage battery's own trajectory, and a UPS day has
  nothing to say about where it would have been. Switching back
  resumes the chain from the last real greedy day, and ADR-0010's
  measured seed corrects it on the first midnight after.
- **Without the ADR-0010 SoC sensor the published trajectory is
  pessimistic** — it seeds at the floor and shows a full refill every
  day. The actuation is correct either way (the window is
  seed-independent), but the SoC forecast and the forecast cost will
  both read high until the sensor is configured. This is the
  strongest argument yet for configuring it.
- **The switch exists in planning-only mode too**, unlike the
  actuation gates: it changes the plan, which planning-only computes.

## Alternatives rejected

**Filter discharge out of the existing plan.** Rejected on constraint
2: a plan whose charge was sized against a discharge that no longer
happens overshoots the ceiling. The bug would appear only in summer,
only after the morning ponta, as a validation failure that silently
stops all actuation.

**A config option instead of a switch.** Rejected because the trigger
for this mode is an event — a storm warning, planned grid work — not
a preference. A config option reloads the entry and cannot be
automated from a weather forecast. The switch restores its state
across restarts, so it covers the permanent case as well.

**Make it a third `dry_run`-style regime.** Rejected: dry-run selects
between two arbitrage plans and lives in config. UPS mode is
orthogonal to that choice and has to win over it.

**Hold the pack at a configurable target below 100%** (kinder to the
cells, cheaper to maintain). Deferred, not rejected — it is a real
refinement, and `ups_plan` already targets `cap_usable_kwh` through a
single expression. Left out of v1 because the owner's stated want is
backup power, and the right default for backup is "as much as
possible".

## Amendment (ADR-0012, 2026-11-15)

A **flat tariff** (fixa simples, Média simples) now also selects the
UPS builder: with every quarter priced alike there is nothing to
arbitrage. The switch is still the owner's single source of truth for
*choosing* UPS — it is never written by the tariff, keeps working on
every periodic tariff, and its state survives a tariff change. The
coordinator exposes `ups_active = ups_mode or tariff.is_flat`, which is
what the executor reads, and the plan sensor reports `ups_reason`
(`switch` or `tariff`).
