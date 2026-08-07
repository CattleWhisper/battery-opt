# ADR-0008: Reserve floor delegated to the battery; no SoC readback

**Status:** Accepted (owner, 2026-08-07) · **Date:** 2026-08
**Amends:** ADR-0006 (its "the integration keeps the reserve-floor
guard" consequence)

## Context

The integration read the Marstek SoC sensor every coordinator refresh
and ran a floor guard in the executor (SoC ≤ 27 % during DISCHARGE →
HOLD, hysteresis +0.15 kWh), documented as the primary floor
protection on the Venus E V3, whose firmware cutoff registers are
MISSING upstream.

A review found the guard unsound as a protection: when the SoC sensor
dies, the coordinator keeps its last value and the executor keeps
actuating against a frozen reading — the guard is blind exactly when
it matters, while its presence makes the system look protected. The
review also showed the SoC path was load-bearing for nothing else:
the plan is a schedule (ADR-0007 — states only), and both run-time
magnitudes are already closed loops owned by the firmware.

## Decision

**The integration reads no SoC, anywhere. The reserve floor is the
battery's to manage.**

- The executor has no floor guard; the coordinator does not poll SoC;
  the driver has no `read_soc`. The SoC sensor leaves the config flow
  (battery group is four entities: force_mode, set_charge_power,
  rs485_control_mode, user_work_mode).
- Run-time floor enforcement is the firmware discharge cutoff (44001,
  written once at setup where the entity exists) — on the V3, where
  the register is MISSING, the device's own internal minimum governs.
- The 27 % reserve floor remains a PLANNING constraint (C-4): every
  plan's modelled trajectory stays at or above it, and daily plans
  are seeded at the floor, not at a live reading.

## Consequences

- On the V3 the effective floor during anti-feed is the firmware's
  internal minimum, which sits BELOW the 27 % planning reserve. The
  owner accepts this: the plan never schedules below 27 %, and what
  the firmware allows past that during anti-feed is the device's
  business.
- Invariant #3 changes meaning: "never plan below the reserve floor"
  (a plan-validation property), no longer a run-time policing duty.
- Health detection latency changes: without a per-tick SoC read, a
  dead battery is noticed at the next failed *transition* (3-strike
  driver policy), not within one coordinator cycle. Acceptable —
  every state is safe unattended by design (spec §8 failure
  semantics).
- Forecast-vs-real SoC comparison is unaffected: dashboards overlay
  `sensor.battery_opt_soc_forecast` on the Marstek integration's own
  SoC sensor directly; this integration never needed to be in that
  path.
