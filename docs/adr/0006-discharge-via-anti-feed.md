# ADR-0006: Discharge via the firmware's anti-feed mode, not force-discharge

**Status:** Accepted · **Date:** 2026-08

## Context

The owner is not paid for export, so zero grid injection is a hard
constraint (invariant #1), and discharge is the only direction with export
risk. Bench tests on the delivered unit (2026-08) identified three usable
control mechanisms: **automatic / anti-feed** (firmware tracks the paired
meter and discharges to match house load — the only mode with native
zero-export), **manual with schedules** (fixed power and direction), and
**manual with external control** (real-time force setpoints or standby).

Force-discharge at a fixed setpoint exports whenever house load drops below
the setpoint. An external zero-export tracking loop over Modbus cannot match
the firmware's reaction time and would merely reimplement anti-feed, badly.
Fixed-power schedules were rejected for the same export risk.

The same bench tests overturned a piece of community folklore: the
force-mode watchdog (reported to self-reset ~15 s after the last command)
**never fires while the HA Modbus integration is polling** — any Modbus
traffic, reads included, resets it. Normal sensor polling is therefore a
free keepalive, which makes standby-based HOLD viable with no dedicated
keepalive writes.

## Decision

One state machine with three battery states, mapped from the plan:

| State | Mechanism | When |
|---|---|---|
| **CHARGE** | External control: force-charge + power setpoint | Plan intervals with `charge_w > 0` |
| **HOLD** | External control: force-mode standby | Neither charging nor discharging is economic |
| **DISCHARGE** | Firmware **anti-feed** mode (release external control, assert anti-feed) | Plan intervals with `discharge_w > 0` |

All transitions go through `marstek_venus_modbus` entities — ADR-0004
(service calls only, never direct Modbus) is unchanged.

## Rationale

- Only anti-feed has native, fast zero-export tracking; delegate the one
  direction that can export to the component that can actually guarantee it.
- Charging can never export, so external force-charge is safe by
  construction and keeps the planner's quarter-hour resolution.
- HOLD prevents the battery from covering cheap-hour house load with energy
  bought for peak hours.

## Consequences

- **Driver semantics change:** "discharge" becomes a mode switch, not a
  power setpoint. Per-quarter discharge magnitude is no longer
  controllable — the plan chooses *which* quarters discharge, not how much.
  With flat ~1 kW house load and C-1 already bounding discharge to net
  load, nothing economic is lost.
- Zero-export enforcement during DISCHARGE is delegated to firmware; the
  integration keeps the reserve-floor guard (SoC at floor → HOLD) because
  invariant #3 is never delegated, and the firmware SOC cutoffs are written
  once at setup as a backstop (spec §8). **Superseded by ADR-0008
  (2026-08-07):** the floor guard was removed and the floor fully delegated
  to the battery — the guard's SoC source froze on sensor death, making it
  blind exactly when needed. The cutoff writes remain; on the V3 (registers
  MISSING upstream) the device's internal minimum governs.
- Watchdog/keepalive semantics become load-bearing: the Modbus
  integration's poll interval must stay well below the watchdog period, and
  the semantics must be re-verified after every firmware OTA (spec §8
  checklist). They are undocumented behaviour on a specific firmware.
- Failure asymmetry is a feature: integration death during DISCHARGE
  leaves anti-feed serving the house (safe, still zero-export); death
  during CHARGE is stopped by the watchdog (pending the kill-test) or at
  worst overshoots at vazio prices — cheap and safe.
- New dependency on paired-meter health during DISCHARGE; V3 units have
  community-reported pairing instabilities. Guarded if the Modbus
  integration exposes an observable; otherwise on-device checklist.

## Amendment (2026-08-11): the kill-test found no watchdog

The spec §8 item 1 kill-test (network cable pulled during an
RS485-controlled force-charge) found **no watchdog at all**: charging
continued for over 2 min after all Modbus traffic stopped. Two
consequences paragraphs above are revised by measurement:

- The keepalive analysis is moot — there is no watchdog period for the
  poll interval to stay below. HOLD persists simply because nothing
  clears external control.
- Integration death during CHARGE is **not** stopped by any watchdog;
  the 42011 charge-to-SoC backstop (verified working, item 3) is the
  load-bearing stop, so `charge_to_soc` is effectively required in
  active mode. Death during HOLD leaves the commanded stop in place —
  still safe, by inertia rather than by watchdog.

Undocumented firmware behaviour either way: re-verify after every OTA
(spec §8). Full results register in `docs/findings.md`.
