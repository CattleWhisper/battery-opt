# Spec: Charge/Discharge Optimiser

**Version:** 0.2
**Status:** draft for review
**Precedes:** `docs/plan.md`

> Constants, glossary and invariants live in `CONTEXT.md`. This document does not repeat them.

---

## 1. Objective

A Home Assistant integration that, each day after OMIE publishes day-ahead prices, computes the optimal charge/discharge plan for the next day's 96 fifteen-minute intervals and executes it through the `marstek_venus_modbus` integration.

**Why:** the static seasonal schedule yields **€196/year measured** (Task 6 backtest; the MA30-era estimate was ~€267). The dynamic optimiser adds **+€131/year measured** (Checkpoint B; estimated €30–80 pre-backtest) by exploiting:

1. picking the cheapest quarter-hour within each period;
2. the seasonal inversion of the charging window (vazio in winter, midday in summer);
3. the decision **not** to cycle on days without sufficient spread.

**Non-goal:** forecasting prices. Next-day prices are the result of an auction that has already cleared. This is deterministic optimisation, not forecasting.

**Non-goal:** replacing the `marstek_venus_modbus` integration. This integration is a **planner**, not a driver (see ADR-0004).

### Success Criteria

| # | Criterion | Verification |
|---|---|---|
| SC-1 | Zero-export respected | Export meter at zero over 30 days |
| SC-2 | Contracted power respected | No breaker trips; `total_load <= 4400 W` in the plan |
| SC-3 | Reserve floor respected | `SoC >= 27%` in every interval |
| SC-4 | Beats the static baseline | Realised saving ≥ static + €30/year |
| SC-5 | Graceful degradation | With no prices, runs the fixed seasonal schedule without intervention, marked `fallback: static` on the plan sensor |
| SC-6 | Ponta covered | Summer months: ≥95% of ponta consumption served by battery or solar. Winter months: tracked against the one-unit deliverable ceiling — 3.46 kWh/day vs the 5.2 kWh weekday demand (~67%, Checkpoint B measurement) — not gated |
| SC-7 | Calendar correct | Weekly totals: 15 h ponta (summer), 25 h (winter) |

---

## 2. Assumptions

**Correct these now, or development proceeds on them:**

1. **Marstek Venus E 3.0** already purchased, 5.0 kWh usable, 2500 W, 90% round-trip.
2. Marstek smart meter installed; **zero-export active**.
3. Contracted power **4.6 kVA**, no intention to increase.
4. Household load **flat at ~1.04 kW**, dominated by AC and homelab running 24/7.
5. Target tariff: **EDP Indexada Horária DD+FE, tri-horária, weekly cycle**.
   A 35% energy discount runs on the current simples tariff for the next
   3 months, during which the fixed tariff beats indexed-plus-battery by
   ~€118/year. The switch decision is taken when that promotion expires;
   at the 15% discount level, switching is worth ~€183/year.
6. Control via **local Modbus TCP**, through the `marstek_venus_modbus` integration.
7. A 460 W balcony solar panel may or may not be installed. The system works either way.
8. **Home Assistant's timezone is Europe/Lisbon** — enforced by the config flow (2026-08-07): the tariff calendar, the OMIE market day and every wall-clock trigger are Portugal-local.

---

## 3. Tech Stack

| Component | Choice | ADR |
|---|---|---|
| Form factor | **Custom integration** (HACS) | ADR-0002 |
| Repository | **Single**, with `core/` free of HA dependencies | ADR-0001 |
| Optimiser | **Own greedy** in v1; EMHASS reconsidered in v2 | ADR-0003 |
| OMIE prices | HA core `omie` integration — the `get_prices_for_date` service (its sensors carry only the current price) | — |
| Battery | `marstek_venus_modbus` via service calls | ADR-0004 |
| Tests | `pytest` + `pytest-homeassistant-custom-component` | — |
| Solar | `forecast_solar` or `solcast` | — |

---

## 4. Price Model

```
price[i] = P_OMIE[i]/1000 x (1 + PERDAS) x K1 + K2 + TAR_energia(period(i))
```

Terms deliberately **excluded** from the optimisation, being uniform constants that cannot change the optimum:

- `K3` (€/day) and `TAR_POTENCIA` (€/day) — additive constants
- `VAT` — uniform multiplier

Include both in the reporting module only.

**Known risk:** on Indexada Horária the loss coefficient varies per quarter-hour rather than being fixed at 16.4%. Overnight losses tend to run below average and peak losses above — which works slightly **against** us, since we charge off-peak and discharge on-peak. Use 16.4% flat in v1 and record the deviation against the invoice.

---

## 5. Tri-horária Weekly Calendar

A pure function, no I/O, versioned by effective date (ADR-0005). See `docs/tariff-reference.md` for the full tables and the mandatory test cases.

Summary:

| | Vazio | Ponta | Cheias |
|---|---|---|---|
| **Summer, Mon–Fri** | 00:00–07:00 | **09:15–12:15** | remainder |
| **Summer, Saturday** | 00:00–09:00, 14:00–20:00, 22:00–24:00 | — | 09:00–14:00, 20:00–22:00 |
| **Winter, Mon–Fri** | 00:00–07:00 | **09:30–12:00, 18:30–21:00** | remainder |
| **Winter, Saturday** | 00:00–09:30, 13:00–18:30, 22:00–24:00 | — | 09:30–13:00, 18:30–22:00 |
| **Sunday** | all day | — | — |

Summer = last Sunday of March → last Sunday of October.

---

## 6. Constraints

| ID | Constraint | Expression |
|---|---|---|
| C-1 | Zero-export | `discharge[i] <= net_load[i]` |
| C-2 | Discharge power | `discharge[i] <= P_DIS_MAX` |
| C-3 | Charge power | `charge[i] <= min(P_CHG_MAX, P_USABLE - house_power[i])` |
| C-4 | Reserve floor | `SoC[i] >= CAP_MIN` |
| C-5 | Ceiling | `SoC[i] <= CAP_USABLE` |
| C-6 | Efficiency | `SoC[i+1] = SoC[i] + charge[i]*eta_c - discharge[i]/eta_d`, `eta_c = eta_d = sqrt(0.90)` |
| C-7 | Exclusivity | `charge[i] * discharge[i] = 0` |
| C-8 | Wear cost | `price[d] > price[c]/eta_rt + WEAR_COST` |

`net_load[i] = max(0, house_load[i] - solar[i])` — solar is self-consumed first; it never charges the battery via the grid.

**Prices may be negative.** OMIE can clear below zero (SDAC floor ~−500 €/MWh); nothing in the constraints or the algorithm may assume `price >= 0` — no `abs()`, no `max(0, price)`. C-8 handles the sign correctly as written: a negative `price[c]` divided by `eta_rt` becomes more negative, which is the right economics. Delivered price stays positive in practice because K₂ and the TAR are added regardless of sign. Full analysis: `docs/findings.md` §Negative OMIE prices.

---

## 7. Algorithm (v1: greedy)

```
1. Compute per-interval capacities (C-1..C-3)
2. REPEAT:
     d = highest-price interval with remaining discharge capacity
     c = lowest-price interval BEFORE d with remaining charge capacity
     IF price[d] <= price[c]/eta_rt + WEAR_COST: STOP
     q = min(discharge_cap[d], charge_cap[c]*eta_rt, energy_available)
     record pair; update residual capacities
3. Check the SoC trajectory (C-4, C-5); drop the least profitable pair if violated
4. Return the plan
```

**Complexity:** O(n²), n = 96 → milliseconds.

**Known limitation:** greedy picks pairs locally. With one battery and 96 intervals, the gap to optimal is typically <2%. If the backtest shows more than €10/year of headroom, reconsider EMHASS (ADR-0003).

**Implementation note (Checkpoint A):** `core/optimiser.py` guarantees C-1..C-7 **by construction** (causality plus ceiling-checked pair sizing) rather than step 3's validate-and-repair; step 3 survives as the executor's independent validation of every plan before actuation (spec §11).

**Charge-window arming (owner decision 2026-08-13, both planners):**
after the solve/window fill, every quarter that could still profitably
feed a remaining discharge stays a CHARGE quarter at a marginal
`ARMED_CHARGE_KWH` (0.04 W state selector, ADR-0007 — not an energy
claim), shaved off the run's own allocation so totals, the trajectory
and the day-chaining seed are conserved. The greedy arms forward past
each run while the C-8 condition holds against the cheapest discharge
still ahead (never past the last discharge); the static plan arms its
whole seasonal window. Rationale: the modelled charge energy is a
floor, not a guarantee — anti-feed discharges track REAL load (often
deeper than forecast) and the ADR-0007 loop throttles under house
load — so the run-time pair (loop at full power + the firmware
percent-target, which self-corrects any start-SoC error) owns the
stop, and shortfalls recover in the armed time instead of persisting
into the next ponta. Cost: the plan may model a few 1e-4 EUR of
insurance; the armed tail appears in `schedule` as ~0 W charge
segments.

---

## 8. Interfaces

### Inputs

| Source | Availability | Fallback |
|---|---|---|
| OMIE D+1 prices | ~13:30 daily | Fixed seasonal schedule |
| TAR calendar | pure function | — |
| Load forecast | median of the last 4 same-weekday occurrences | flat 1.04 kW |
| Solar forecast | `forecast_solar` | 0 |

The SoC is deliberately NOT an input (ADR-0008, owner 2026-08-07):
no SoC is read anywhere. The day seed follows the ACTUATED regime
(revised 2026-08-13 with Task 12). **Under dry-run (static
actuation):** every plan — the executor's static plan, the static
fallback, the advisory greedy and tomorrow's preview — seeds its day
at the previous weekday's PLANNED static end SoC (virtual
day-chaining, `core.static_schedule.chained_start_soc`, 2026-08-07/08):
the summer schedule charges at midday AFTER the morning ponta, so a
floor-seeded summer day can never discharge — the battery would sit
full all summer and Checkpoint C's ponta-coverage criterion could not
be met, and a floor-seeded advisory trajectory would never match the
real SoC it exists to be compared against. The seed is the plan's own
model rolled forward (exact: every weekday's charge window fills to
capacity from any start, so its end is start-independent; weekends
are no-ops that pass SoC through), never a readback — ADR-0008
stands. In winter the previous weekday ends drained, so the chained
seed IS the floor. **With dry_run off (greedy actuation):** every day
seeds at the reserve floor instead — a greedy day ends AT the floor
by construction (the single-day model values no stored energy:
everything above the floor is sold wherever price beats wear), so the
static chain's "full" seed would model energy the battery does not
have; floor-seeded, the solve buys the night's cheap quarters before
the morning ponta. (Extreme negative-price days can leave the real
end above the floor — the battery then holds more than modelled, the
safe direction, corrected within a day.) Daily savings keep the
backtest's convention: charge cost books on the day it is bought,
discharge revenue on the day it is sold.

### Outputs (entities)

| Entity | Description |
|---|---|
| `sensor.battery_opt_plan` | Current action; attributes carry `schedule` — the advisory plan as merged charge/discharge windows `{start, end, direction, power_w}` (ISO timestamps with offset, hold windows omitted), one flat list spanning today and, once D+1's own price series builds, tomorrow (seeded at the reserve floor, not chained from today — decision 9) — and `static_schedule`, the static baseline in the same format over the same span, for the greedy-vs-static comparison graph (the Task 12 dry-run view) |
| `sensor.battery_opt_forecast_savings` | Forecast saving vs. not cycling |
| `sensor.battery_opt_vs_static` | **Forecast gain vs. the fixed schedule — the metric that justifies the project** |
| `sensor.battery_opt_realised_savings` | Ex-post realised saving today from MEASURED battery flows (Task 13): power-sensor slices valued at the delivered price of their instant, wear at TRUE wear per discharged kWh (no SoC — ADR-0008); month-to-date realised/forecast and their deviation in attributes; unavailable without `CONF_BATTERY_POWER_SENSOR` |
| `sensor.battery_opt_current_price` | Delivered price now per the EDP Indexada formula (€/kWh, excl. fixed terms and VAT); declared like core OMIE's price sensor so the Energy dashboard accepts it as a grid price entity; attributes carry `prices` — merged segments `{start, end, price_eur_kwh, tar_period}` (split at every TAR boundary so each value is checkable against the tariff table), spanning today and, once D+1's own price series builds, tomorrow |
| `sensor.battery_opt_soc_forecast` | Planned SoC for the current quarter-hour (%, same unit as the battery's own SoC sensor, for direct forecast-vs-real comparison — the Checkpoint C soak metric); full day trajectory (97 boundary values, kWh and %) in attributes, sourced from the executor's actuated plan when a battery runs, the advisory plan otherwise — plus both plans separately as `greedy_trajectory_pct` (None in the static fallback) and `static_trajectory_pct`, for the real-vs-greedy-vs-static overlay |
| `sensor.battery_opt_load_mae` | Mean absolute error (W) of yesterday's load forecast vs. observed, computed at day close; unavailable until a load meter is configured and one full day has closed (plan Task 11) |
| `sensor.battery_opt_cost_today` | Grid-import cost today, EUR, excl. VAT (Task 13 pulled forward): variable = Σ(meter delta × delivered price at that instant, negative deltas from a meter reset counting as 0) + the daily fixed term (K3 + TAR potência); `state_class` TOTAL, `last_reset` at local midnight; attributes `variable_eur`, `fixed_eur`, `energy_today_kwh`; unavailable without a configured grid-import energy sensor |
| `binary_sensor.battery_opt_healthy` | With a battery: the executor's safe-to-actuate latch — off on an invalid plan or a three-strike driver failure. Missing prices are NOT unhealthy there: the static fallback still actuates, marked `fallback: static` (SC-5). Planning-only: off when no price vector can be built, since plans are then impossible |
| `switch.battery_opt_executor_actuation` | Manual override (default on, restored across restarts): off = the executor keeps planning and validating but skips every driver write; its commanded state is forgotten while off, so re-enabling replays the full transition — safe after manual battery control. Active mode only |
| `switch.battery_opt_charge_loop_actuation` | Manual override for the charge-power loop: off = keeps computing (fallback flag stays live), writes no setpoints. Exists only when the loop's sensors are configured |
| `button.battery_opt_recalculate_plan` | Immediate full recomputation (direct refresh: refetch prices, rebuild the load forecast, re-solve today + tomorrow's preview); recomputes only, never actuates |
| `button.battery_opt_apply_plan` | Runs a real executor tick now instead of at the quarter boundary — validation, the override gate and the health latch all apply; idempotent via the write-once tracking. Active mode only |

### Actuation — control state machine (ADR-0006)

Service calls against `marstek_venus_modbus` entities. **Never direct
Modbus writes** (ADR-0004). One state machine, three battery states,
mapped from the plan each interval:

| State | Mechanism | Plan mapping |
|---|---|---|
| **CHARGE** | External control: force-charge; power owned by the charge-power loop (ADR-0007, Task 15) | `charge_w[i] > 0` |
| **HOLD** | External control: force-mode standby | `charge_w[i] = discharge_w[i] = 0` |
| **DISCHARGE** | Firmware **anti-feed** (auto zero-export via the paired meter) | `discharge_w[i] > 0` — the value selects the quarter; magnitude is the firmware's |

**The plan carries states only (ADR-0007, owner 2026-08-07).** The
optimiser's power vectors remain its internal energy accounting
(C-3..C-6 need a power model), but actuation reads them solely as
state selectors: `> 0` or not. Both run-time magnitudes are closed
loops — discharge against the meter (firmware anti-feed), charge
against the contracted-power ceiling (the loop below).

**Executor plan source (Task 12, shipped 2026-08-13; `dry_run`
default ON):** with `dry_run` on, the executor actuates the chained
static schedule and the coordinator's greedy stays advisory — the
permanent dry-run. With it off, the executor adopts the coordinator's
validated greedy for the day, published as `DynamicDayPlan` — the
plan together with the params and load/solar vectors it was built
with, so the executor's per-tick re-validation uses the same inputs
(validating against the executor's own flat load would produce false
C-1 violations). It falls back to the chained static whenever no
trustworthy greedy exists (prices missing, failed solve), and a
fallback day upgrades on the first tick after the greedy appears. An
invalid dynamic plan is demoted to static, never actuated (decision
6), notified at most once a day. The plan sensor's
`executor_plan_source` attribute reports `static` / `greedy` /
`static-fallback`. Flip `dry_run` only after the Checkpoint C review,
having first compared a week of the advisory schedule against actual
behaviour (plan Task 12 verification).

**Transitions** (entity operations; underlying registers noted for
cross-checking against community sources):

- **→ CHARGE:** `rs485_control_mode` on (42000) → `set_charge_power`
  (42020) → `charge_to_soc` backstop (42011, see below) → `force_mode` =
  charge (42010=1).
- **CHARGE/HOLD internal:** setpoint changes are single writes;
  `force_mode` charge ↔ stop.
- **→ DISCHARGE:** `force_mode` = stop (42010=0) → `rs485_control_mode`
  off (42000) → `user_work_mode` = anti-feed (43000=1), **re-asserted on
  every transition** — entering force mode flips the work mode to manual
  (item 2, confirmed on-device). Releasing external control *appeared*
  to restore anti-feed on the current firmware, but nothing is assumed:
  the re-assert stays.
- **DISCHARGE → HOLD:** `rs485_control_mode` on (42000) → `force_mode` =
  stop (42010=0).

**Charge stop policy (decided 2026-08-07):** time-boxed — the plan says
which quarters are CHARGE and the executor exits the state on schedule.
Additionally write `charge_to_soc` (42011) = the window's planned end
SoC (small margin, capped at 100) as a firmware backstop against the
integration dying mid-window. Item 3 confirmed on-device that 42011
coexists with force-charge, so the backstop is active — and since the
kill-test found **no watchdog** (item 1, 2026-08-11), it is the
**load-bearing** stop for a dead integration mid-charge, not
belt-and-braces: treat `charge_to_soc` as required in active mode.
(No SoC readback confirms the stop — ADR-0008.)

**Charge-power control loop (ADR-0007, Task 15 — decided and
implemented 2026-08-07; bench verification pending):** while the state
machine is in CHARGE, a loop faster than the 15-minute executor owns
the `set_charge_power` setpoint:

```
other_load = measured_grid_import_w - battery_charge_w
setpoint   = clamp(P_USABLE_W - other_load, 0, 2500)   # floor to 50 W
```

- Triggered by grid-import power sensor updates; rate-limited (min
  ~5 s between writes) with a ~100 W deadband so meter noise does not
  chatter the register (42020 is volatile — no EEPROM concern, only
  churn).
- `P_USABLE_W = 4400` keeps the standing 200 W margin against the
  4.6 kVA contract (invariant #2, now enforced against MEASUREMENT).
- Inputs: a grid-import power sensor (W) and the battery's own power
  sensor (to subtract its draw from the import reading) — both new
  optional config entities; the loop only runs when both are set. The
  battery sensor follows the HA battery convention (positive =
  discharging, owner 2026-08-11); the HA wiring negates it, so the
  loop's `battery_charge_w` and `core.reporting` stay charge-positive.
- Fail safe: either sensor unavailable → fall back to a conservative
  static 2000 W (the previously proven value), flagged in the plan
  sensor's attributes; sensor recovers → loop resumes.
- Entry setpoint on → CHARGE: the loop's current computed value when
  available, else the 2000 W fallback.
- Planning-side C-3 uses `min(2500, P_USABLE - forecast_load)` as the
  per-quarter charge capacity — an energy-model estimate; the loop is
  the enforcement. The old static 2000 W ceiling is superseded (the
  §11 "ask first: charging above 2000 W" — this decision is the owner
  asking).

**Firmware SOC cutoffs (decided 2026-08-07):** `charging_cutoff_capacity`
(44000) = 100 % and `discharging_cutoff_capacity` (44001) = 27 % are
written **once at integration setup** — never per transition; the 44xxx
block is EEPROM-backed, and equal values are never rewritten
(compare-before-write). The discharge cutoff IS the run-time floor
mechanism (ADR-0008). **Implementation finding (2026-08-07):** the
upstream register map lists both cutoff numbers as MISSING on the Venus
E V3 — the entities do not exist there at all, so on V3 the cutoff
config fields stay empty, the setup write is skipped with a log line,
and the device's own internal minimum governs during anti-feed.

**Guards during DISCHARGE (revised 2026-08-07, ADR-0008):** zero-export
AND the reserve floor are both delegated to the firmware. The original
integration-level SoC floor guard was removed: it read the SoC through
the coordinator, which keeps its last value when the sensor dies, so
the guard was blind exactly when it mattered while making the system
look protected. The floor is a planning constraint (C-4); at run time
the battery manages it. A meter-pairing guard is added only if the
Modbus integration exposes a health/pairing observable; until then,
pairing-loss behaviour is verify-on-device item 6 — answered
2026-08-11: pairing loss stops the output dead (no CT data, no
output), the safe behaviour.

**No watchdog on this firmware (measured 2026-08-11, item 1):** the
community-reported ~15 s force-mode watchdog never fired in a
network-cut test — force-charge continued for over 2 min with all
Modbus traffic stopped. The keepalive constraint on the scan interval
is therefore moot (polling cadence is a data-freshness concern only),
and nothing stops a runaway CHARGE but the 42011 backstop and the
executor's own time-boxing. Still over the single Modbus TCP
connection the unit accepts; write setpoints once, rewrite only on
decision changes. Re-measure after every OTA — a future firmware may
(re)introduce the watchdog.

**Failure semantics (asymmetric by design; revised 2026-08-11 after
item 1):** integration dead during DISCHARGE → battery stays in
anti-feed: zero-export, still serves the house — safe. Dead during
CHARGE → **no watchdog stops it**: the charge runs on until the 42011
charge-to-SoC backstop target — bounded and cheap, but only because
the backstop is written on every CHARGE entry. Dead during HOLD → the
battery stays in the commanded external-control stop, doing nothing —
HOLD survives.

**On-device verification checklist** (run before enabling actuation, and
**re-run after every firmware OTA** — the watchdog and mode-flip semantics
are undocumented firmware behaviour):

Executed 2026-08-11 — all seven answered; full results register in
`docs/findings.md` and `docs/validation-checklist.md`:

1. Kill the integration with force-charge active; time the self-stop
   (~15–30 s expected). Determines whether shutdown safety writes are
   belt-and-braces or load-bearing. — **No self-stop within 2 min: no
   watchdog. The 42011 backstop is load-bearing**
2. Confirm: 43000 is writable directly; entering force mode flips 43000
   to manual; releasing external control alone does *not* restore
   anti-feed. — **Confirmed writable and the force-mode flip; releasing
   external control appeared to restore anti-feed, but the per-transition
   re-assert stays (assume nothing)**
3. Confirm 42011 works alongside force-charge (charge stops at target).
   — **Confirmed**
4. Confirm 44001 accepts the 27 % write on this firmware. — **Not
   exposed for the V3 (upstream MISSING) — the expected clean absence;
   the device's internal minimum governs**
5. DISCHARGE → HOLD: anti-feed disengages cleanly when external control
   takes over. — **Confirmed**
6. Meter-pairing loss during anti-feed: does discharge stop dead (safe)
   or misbehave? — **Stops dead (safe)**
7. Confirm polling at the configured scan interval suppresses the
   watchdog indefinitely. — **Moot: no watchdog (item 1)**

---

## 9. Triggers

| Time | Action | On failure |
|---|---|---|
| 13:45 | Fetch D+1 prices, compute, archive | Retry 14:15, 15:00, 16:00; then fixed schedule marked `fallback: static` (planning-only additionally reports `healthy=off`; with a battery, healthy stays the executor's latch and static actuation continues — see §8) |
| 00:00:30 | Refresh so today's vector loads seconds after the date change (the 15-min poll would otherwise lag, blanking the price sensor and the cost tracker's lookup) | — |
| Every 15 min | Apply the current interval's state (CHARGE setpoint / HOLD / DISCHARGE) | 3 Modbus failures → `healthy=off` + notification |
| 00:05 | Close the day: load archive + MAE (the realised tracker folds its day at its own 00:00 roll) | — |
| 00:00 on month change | Monthly reconciliation report (Task 13): persistent notification with realised vs forecast savings; deviation beyond ±10% flagged. Fired by the realised tracker's day roll; a restart landing in a new month sends it late rather than never | — |
| 02:00 daily | If today is the last Sunday of Mar/Oct: persistent notification prompting manual verification of the seasonal switch | — |

*(A 23:55 plan-vs-real-SoC validation row was removed 2026-08-07: obsolete under ADR-0008 — no SoC is read.)*

---

## 10. Testing Strategy

| Level | Target | Method |
|---|---|---|
| **Unit** | `calendar.py` | Mandatory test table (`docs/tariff-reference.md`). Verify weekly totals |
| **Unit** | `prices.py` | Reconstruct a known price from real OMIE data and compare against the invoice |
| **Property** | `optimiser.py` | No plan violates C-1..C-7, over 1000 random days |
| **Property** | `optimiser.py` | Saving ≥ the fixed schedule's saving, always |
| **Backtest** | system | 12 months of OMIE; compare dynamic vs. static vs. no-cycling |
| **Integration** | driver | Fake driver; verify the service-call sequence |
| **Unit** | state machine | Every transition sequence and guard (spec §8), against a fake driver |
| **Acceptance** | production | Real invoice vs. `reporting.py`, monthly |

---

## 11. Boundaries

**Always**
- Validate the plan against every constraint **before** actuating
- Archive every plan and every outcome — without this there is no backtest and no second-unit evaluation
- Keep the fixed seasonal schedule working and tested
- Record the deviation between forecast and invoiced saving

**Ask first**
- Increasing contracted power
- Lowering the reserve floor below 27%
- ~~Charging above 2000 W~~ — asked and decided (owner, 2026-08-07,
  ADR-0007): the run-time limit is the charge-power loop against the
  measured import, up to the device's 2500 W
- Enabling additional cycles into cheias

**Never**
- Export to the grid, even at a favourable price
- Actuate without checking `binary_sensor.battery_opt_healthy`
- Open a second Modbus connection
- Hardcode the calendar to the current year
- Import `homeassistant` inside `core/`

---

## 12. Open Questions

1. ~~OMIE granularity~~ **RESOLVED**: OMIE publishes quarter-hourly prices
   (H1Q1 = 00:00–00:15 CET) since the SDAC 15-minute MTU go-live on
   2025-10-01. 96 periods/day, matching EDP's billing granularity. The
   backtest has true quarter-hourly data for 11 of 12 months; Sep-2025 is
   hourly-only. Production half **RESOLVED** (2026-08-05, superseded
   2026-08-06): first verified against the HACS `omie` integration
   (luuuis/hass_omie — quarter-hourly `today_hours`/`tomorrow_hours`
   attributes); the shell then moved to **HA core's `omie` integration**,
   whose `get_prices_for_date` service returns the quarter-hourly day
   series per market date (its sensors carry only the current price) —
   the source `prices_source.py` reads today.
2. **Variable losses.** Where does E-Redes publish the quarter-hourly loss profiles?
3. ~~Zero-export~~ **RESOLVED** (owner, 2026-08-05): enforced internally by
   the device via its smart meter. C-1 in the plan is a defensive check;
   the executor still validates every plan before actuating (defence in
   depth). See `docs/findings.md` §Checkpoint B decisions.
4. ~~End-of-day SoC target~~ **RESOLVED** (Task 6): not material. A 13:00
   planning boundary — the proxy for letting midday charge serve the next
   morning — adds €4.95/yr (+1.5%) and cuts cycles 417→354. Midnight
   planning with SoC chaining stands for v1; a 48 h horizon is an
   efficiency refinement, not a different answer. See `docs/findings.md`.
5. **2027 calendar.** The ERSE reform has no published dates or hours yet.
6. **Solar interaction.** Validate the model (solar as load reduction) against real data after installation.
