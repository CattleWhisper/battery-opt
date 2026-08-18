# ADR-0009: Multi-battery — subentries, fleet driver, one virtual battery

**Status:** Accepted — implementation deferred until a second unit exists · **Date:** 2026-08

## Context

Checkpoint B evaluated a second Venus E by doubling `cap_usable_kwh`
(`backtest/run.py --cap 10`): **+€110.64/yr, ~12.7-year payback — not
purchased** (docs/findings.md). The binding constraint is the household
load (1.04 kW), not capacity. The revisit trigger is the ERSE 2027
reform. This ADR records the architecture *now*, while the code map is
fresh, so the revisit starts from a settled design instead of a blank
page.

Today everything battery-shaped is singular: one flat `MarstekEntities`
group (config_flow `BATTERY_ENTITY_KEYS`, all-or-none), one
`MarstekDriver` (its own three-strike counter and commanded-state
cache), one executor, one charge-power loop, one device page in the UI
(`entity.py`). The planner, by contrast, is already fleet-capable —
`BatteryParams` capacity and power limits are parameters, never
constants (ADR-0003's Checkpoint B lever). The pinned Home Assistant
(2026.6.4) supports **config subentries**
(`homeassistant.config_entries.ConfigSubentry` / `ConfigSubentryFlow`).

## Decision

1. **One config subentry per battery.** Each subentry holds that unit's
   seven entity pickers (mode select, charge power number, RS-485
   switch, work mode select, charge-to-SoC, the two cutoffs), its
   battery-power sensor, and its per-unit physical parameters —
   **usable capacity and standby self-discharge** (owner 2026-08-18:
   per-unit values, summed in code). The parent entry keeps everything
   house-level: grid power sensor, load sensor, grid energy meter,
   tariff parameters, dry-run. The existing flat entity group migrates
   into subentry #1 via a config-entry version bump.
2. **One `marstek_venus_modbus` instance per unit.** ADR-0004's
   one-connection rule is per *device* and extends unchanged: each
   battery's Modbus connection is owned by its own copy of the Marstek
   integration. battery_opt still never opens Modbus — it fans service
   calls out to N entity sets.
3. **Fleet driver, all-healthy gate.** One `MarstekDriver` per subentry
   (each keeps its own three-strike counter and state cache) behind a
   thin broadcast wrapper. Fleet health = **all** units healthy: any
   unit striking out flips the health latch and safe-stops the whole
   fleet — with no firmware watchdog (spec §8), a half-commanded fleet
   is worse than a stopped one.
4. **One virtual battery for planning.** Capacities, floors, standby
   drains and device power limits sum into a single `BatteryParams`;
   the optimiser, the static schedule, the chaining and the validators
   run unchanged — exactly how the backtest already models the second
   unit. One executor state machine commands the fleet: all units
   charge, hold or discharge together.
5. **One charge-power loop, acting as allocator with
   capacity-proportional shares** (owner 2026-08-18). The contracted
   ceiling is the only genuinely shared resource. The single loop
   computes `other_load = grid_import − Σ(battery powers)` (hence every
   subentry must contribute its power sensor) and one total target
   `min(Σ device maxes, P_USABLE − other_load)`, then gives each unit
   its capacity share of it:

   ```
   unit_setpoint = total_target × cap_unit / Σ caps
   ```

   e.g. 3 800 W of headroom across a 5 kWh and a 7 kWh unit →
   5/12 × 3800 ≈ 1583 W and 7/12 × 3800 ≈ 2217 W. A share that
   exceeds a unit's device max is capped there and the excess
   redistributed to units with headroom. **Two independent loops are
   rejected**: each would count the other unit's draw as house load —
   a mutual-inflation feedback that oscillates.

## Rationale

- Subentries give the "Add battery" UX, per-unit device pages and clean
  migration without a second config entry — and a second *entry* is the
  wrong shape anyway: two planners would fight over one tariff, one
  house load and one contracted ceiling.
- The aggregate-planner choice keeps every validated core path —
  optimiser, static chain, arming, drain, savings — byte-identical for
  N=1 and untouched for N=2; only the shell fans out.
- All-healthy fleet gating is the conservative reading of the
  no-watchdog finding: partial actuation leaves one unit running a
  stale command with nothing to stop it but the 42011 backstop.
- Capacity-proportional charge shares mean every unit charges at the
  same C-rate, so the fleet's SoC percentages stay in lockstep — the
  aggregate kWh→% conversion behind the per-unit charge-to-SoC
  backstop stays a single shared percentage, and the one-virtual-
  battery model stays honest even with different-sized units.

## Consequences

- Per-subentry device pages carry per-unit `healthy`/status entities;
  realised savings either per unit (own tracker per power sensor) or
  aggregated from the summed sensors. House-level sensors (plan,
  prices, best periods, savings, cost) stay on the parent device.
- The charge-to-SoC backstop percent is written per unit; the
  proportional charge shares keep unit SoC percentages aligned, so one
  shared target percentage serves the fleet. Discharge is anti-feed
  (firmware-driven per unit), so discharge shares are the firmware's —
  per-unit SoC can drift there; each charge window's per-unit
  full-targets re-align it. Heterogeneous *sizes* are handled by the
  proportional rule; heterogeneous *models* (different device maxes,
  efficiencies) stay a bench question.
- Standby self-discharge scales with the fleet (~38 W for two units) —
  per-subentry values summed into the aggregate params (decision 1).
- **Bench gates before any fleet actuation** (spec §8 checklist re-run
  on the fleet): whether two units in simultaneous anti-feed fight over
  the same meter (the classic multi-inverter zero-export interplay);
  the no-watchdog behaviour re-checked per unit and firmware; the
  ADR-0003 "<2% gap to optimal" claim re-verified at 10 kWh (the
  backtest suggests it holds).
- Nothing is implemented until a second unit is purchased; this ADR is
  the design of record for that day.
