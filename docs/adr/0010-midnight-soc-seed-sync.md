# ADR-0010: Midnight SoC sync — the day's planning seed is measured

**Status:** Accepted (owner, 2026-08-31) · **Date:** 2026-08
**Amends:** ADR-0008 (its "the integration reads no SoC, anywhere"
decision, for this one narrow purpose)

## Context

Observed in production (owner, 2026-08-31): the battery began the day
holding more charge than the model believed. At 11:00 the plan had it
at the reserve floor and stopped discharging, while the real battery
was at 44%. Ponta runs 09:15–12:15 at a flat TAR, so the remaining
hour of the most expensive window of the day was served from the grid
while the battery sat on usable charge.

The mechanism is in `core/static_schedule.py`: each ponta quarter
discharges `min(p_discharge_max, net_load, available_kwh)`, where
`available_kwh` derives from the MODELLED SoC. Once the model reaches
`cap_min_kwh` the quarter's discharge is 0, and the executor reads
that as HOLD. The real SoC is invisible to it.

The charge side already solved the mirror problem.
`_arm_window_tail` keeps CHARGE armed through the whole window with a
token state selector and lets the firmware charge-to-SoC target
(42011) stop at ACTUAL full — explicitly "so real-world shortfalls
recover" and to "self-correct any start-SoC error". Discharge has no
equivalent, and cannot get one the same way: the discharge cutoff
register (44001) is MISSING on the Venus E V3, so nothing in the
firmware would stop an armed discharge at the reserve floor. It would
run to the device's internal minimum, silently spending the outage
reserve.

So the error had to be attacked at its source — the seed — and that
requires knowing where the battery actually is.

## Decision

**Once per day, inside a bounded window after local midnight, the
integration reads the battery's SoC and uses it as that day's
planning start SoC.** Everything else in ADR-0008 stands.

- Scope is the seed and nothing else. It replaces `chained_start_soc`
  (static regime), the floor default (dynamic regime) and yesterday's
  recorded greedy end. Today's own pinned record still wins, so
  intraday refreshes never flip the seed.
- Captured at most once per day, only while
  `now < SOC_SYNC_WINDOW_MINUTES` past local midnight. Outside the
  window "now" is no longer the day's START; a 14:00 reading would
  silently rewrite the morning that already happened. The captured
  value is persisted, so a restart later in the day keeps the
  morning's seed instead of reverting to the chain.
- Freshness is judged on the state's `last_reported`, not
  `last_changed` or `last_updated`: HA advances it on every write even
  when the percentage is unchanged, so a battery resting at a flat SoC
  overnight stays valid while a dead integration goes stale within
  `SOC_SYNC_MAX_AGE_MINUTES`.
- All-or-nothing across the fleet (ADR-0009): the seed is one
  aggregate energy figure, and a partial read would under-seed the day
  and plan a discharge the battery cannot deliver.
- The seed is clamped into `[cap_min_kwh, cap_usable_kwh]`. The real
  battery may legitimately sit below the planning floor (ADR-0008: the
  firmware minimum is lower), but a plan seeded below C-4 would be out
  of bounds from its first interval.
- Opt-in by construction: the feature activates only when a SoC sensor
  is configured. Unset, unreadable, stale, out of range, or past the
  window — every path falls back to the modelled chain, exactly the
  behaviour from before this ADR.

## Why this does not reintroduce what ADR-0008 removed

ADR-0008 deleted an executor floor GUARD whose SoC source froze on
sensor death, leaving it "blind exactly when it matters, while its
presence makes the system look protected." That argument is about a
safety mechanism, and it does not transfer to a planning input:

1. **Planning only.** A wrong seed yields a suboptimal plan; it cannot
   produce an unsafe action. Zero-export is still firmware anti-feed,
   the contracted-power ceiling is still the charge loop's, and the
   reserve floor is still the battery's.
2. **Staleness is actually detected** (`last_reported`), which is
   precisely the check the old guard lacked.
3. **It fails to the model, not to a stale number.**

## Consequences

- The integration reads SoC again, in exactly one place
  (`soc_sync.py`), at most once per day. ADR-0008's blanket
  "anywhere" no longer holds literally; its rationale is preserved by
  the three properties above.
- `CONF_SOC_SENSOR` returns to the config flow as an optional
  per-battery field, having been legacy dead config since 2026-08-07.
- Forecast-vs-real SoC divergence should shrink at the START of each
  day. Intraday drift is untouched by design — that is load-forecast
  error, addressed separately (the trailing-week/quarter-resolution
  forecast work), and the owner's position (2026-08-31) is that
  growing intraday drift is a signal to fix the load forecast, not to
  chase the battery with the seed.
- The 27% planning reserve is unchanged. This ADR deliberately does
  NOT extend discharge past the modelled floor; it only stops the
  model from being wrong about where the day began.

## Alternatives rejected

**A. Arm the ponta tail, no SoC** — the exact mirror of
`_arm_window_tail`: keep DISCHARGE armed through every still-profitable
ponta quarter and let run-time stop it. Smallest change, no new
dependency, and it self-corrects a low seed the same way the charge
side does. Rejected because the V3 has no working discharge cutoff
register: nothing would stop the drain at 27%, so the battery would
run to its internal minimum on every day the model was CORRECT,
silently trading the outage reserve for arbitrage. That is a reserve
change, and spec §11 lists lowering the floor as ask-first. Revisit if
a firmware update exposes 44001.

**B. Run-time SoC-aware discharge extension** — while the plan says
HOLD inside a still-profitable window and measured SoC is above the
floor plus a margin, command DISCHARGE instead. This captures the
value continuously rather than only correcting the seed, and it
enforces the floor explicitly (doing what the missing firmware
register cannot). Rejected FOR NOW as the larger step: it puts SoC
back on the run-time control path, which is what ADR-0008 removed, and
it needs its own staleness and hysteresis design. The owner's call
(2026-08-31) was to take the seed fix first and see how much drift
remains. If intraday drift keeps growing after the load-forecast work,
this is the next candidate — with the caveat that a persistent gap is
better read as evidence the load forecast is wrong.
