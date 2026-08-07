# ADR-0007: Charge power by closed loop on measured import, not plan setpoints

**Status:** Accepted (owner, 2026-08-07) · **Date:** 2026-08

## Context

Until now the plan carried per-quarter power setpoints and the executor
wrote them to the battery. The charge values were derived at plan time
from C-3 with a STATIC assumption: `P_CHG_MAX = 2000 W`, chosen as
"contracted power minus assumed flat house load minus margin". Both
directions of that assumption are wrong in practice:

- When the house load spikes above the assumption (AC compressor), a
  planned 2000 W charge can push total import past the contracted
  4.6 kVA — the static margin is a guess, not a guarantee.
- When the house load is below the assumption, charging is throttled
  for no reason — the device can take 2500 W, and every extra watt in
  the cheap window shortens it.

Discharge already solved the symmetric problem by delegation (ADR-0006):
the firmware's anti-feed mode closes the loop against the meter, so
discharge is always "as much as possible without exporting". Charging
has no firmware equivalent for the contracted-power limit — but the
integration can close that loop itself with a fast setpoint controller.

## Decision

**The plan carries states only: CHARGE / HOLD / DISCHARGE. No power.**

- The optimiser still models energy internally (C-3 capacity per
  quarter, SoC trajectory C-4..C-6) — planning needs an energy model —
  but the actuation contract is only *which quarters are in which
  state*.
- **Charging is "as much as possible"**: a dedicated charge-power
  control loop, faster than the 15-minute executor, continuously sets
  the charge setpoint to the highest value that keeps measured total
  grid import under the contracted-power ceiling.
- **Discharging is "as much as possible until zero export"**: unchanged,
  firmware anti-feed (ADR-0006).

Control law of the loop, evaluated on every grid-import power update
(rate-limited):

```
other_load = measured_grid_import_w - battery_charge_w
setpoint   = clamp(P_USABLE_W - other_load, 0, P_DEVICE_MAX_W)
```

floored to the 50 W register step, written only when it moves by more
than a deadband. `P_USABLE_W = 4400` keeps the existing 200 W margin
against the 4.6 kVA contract; `P_DEVICE_MAX_W = 2500` is the device
limit. The loop runs only while the state machine is in CHARGE.

## Rationale

- Closing C-3 against the measured import turns invariant #2 from a
  planning assumption into an enforced property — same philosophy as
  delegating zero-export to the meter-tracking firmware.
- The static 2000 W ceiling existed only because the margin was
  computed blind. With measurement in the loop the ceiling becomes the
  device's own 2500 W, and the cheap-window charge time drops ~20%.
- Spec §11 listed "charging above 2000 W" as ask-first; this decision
  is the owner asking. The run-time limit is now the loop, not a
  constant.

## Consequences

- The executor's CHARGE entry no longer carries a plan-derived power;
  the loop owns the setpoint from entry to exit. The `charge_to_soc`
  firmware backstop (spec §8) is unaffected.
- Planning uses `min(2500, P_USABLE - forecast_load)` as the C-3
  capacity — an estimate for energy accounting only; reality is
  enforced by the loop. Plan-vs-real divergence surfaces in
  `sensor.battery_opt_soc_forecast` (charging faster or slower than
  planned).
- New inputs: a grid-import POWER sensor (W, required for the loop) and
  a battery power sensor (to subtract the battery's own draw from the
  import reading). Sensor unavailable → fail safe: fall back to a
  conservative static setpoint (2000 W, the previously proven value)
  and flag it.
- More frequent writes to the (volatile) charge-power register — no
  EEPROM concern, but the loop needs a deadband and a minimum write
  interval so it does not chatter on meter noise.
- The watchdog/keepalive analysis (ADR-0006) is unaffected: the loop
  only ever writes while external control is already engaged.
