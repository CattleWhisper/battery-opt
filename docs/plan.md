# Implementation Plan

**Preceded by:** `docs/spec.md` · **Context:** `CONTEXT.md`

---

## Overview

Four phases. Phase 0 runs **entirely outside Home Assistant** and answers the three open questions (is the dynamic optimiser worth it? is a second unit worth it? is Horária the right tariff?) before any integration code exists. Only then is the HA shell built.

This is not excessive caution: the total gain of dynamic over static is €30–80/year. If the backtest shows €15, the correct decision is to run the fixed schedule and stop.

---

## Architecture Decisions

See `docs/adr/`. In brief:

- **ADR-0001** — Single repository; `core/` free of HA dependencies
- **ADR-0002** — Custom integration, not pyscript or AppDaemon
- **ADR-0003** — Own greedy before EMHASS
- **ADR-0004** — Planner, not driver: never open Modbus directly
- **ADR-0005** — Tariff calendar versioned by effective date

---

## Dependency Graph

```
calendar.py  --+
               +--> prices.py --> optimiser.py --> backtest/run.py   [PHASE 0]
OMIE data    --+                       |
                                       +--> coordinator.py --> sensor.py   [PHASE 1-2]
forecast.py  --------------------------+          |
                                                  +--> executor --> driver.py
```

Build bottom-up. `calendar.py` first, always — it is the most likely source of error in the system and everything depends on it.

---

## PHASE 0 — Observation (no Home Assistant)

**Goal:** produce real numbers for the three open decisions.
**Deadline:** end of the 35% promotional period (~3 months).
**Note:** the battery cannot earn anything on the current simples tariff.
This window is for validating automations and producing Checkpoint B numbers,
not for capturing savings.

### Task 1: Tariff calendar

**Description:** Pure function `period(dt) -> Literal["ponta","cheias","vazio"]` for tri-horária weekly, with season detection and a structure versioned by effective date.

**Acceptance criteria:**
- [x] Returns the correct period for any `datetime`
- [x] Detects summer/winter from the daylight-saving switches (last Sunday of March/October)
- [x] `CALENDARS = [(effective_date, table), ...]` structure allows adding 2027 without code changes

**Verification:**
- [x] `pytest tests/test_calendar.py` — the case table from `docs/tariff-reference.md`
- [x] Weekly totals: 15 h ponta in summer, 25 h in winter, computed by iterating a full week
- [x] Exact boundaries: 09:14 vs 09:15, 12:14 vs 12:15, 18:29 vs 18:30

**Dependencies:** None
**Files:** `custom_components/battery_opt/core/calendar.py`, `tests/test_calendar.py`
**Scope:** S

---

### Task 2: Price model

**Description:** `price(omie_eur_mwh, dt) -> float` implementing the EDP formula, with parameterisable constants so Horária (K₁=1.08) and Média (K₁=1.10) can be compared.

**Acceptance criteria:**
- [x] Implements `OMIE/1000 * (1+PERDAS) * K1 + K2 + TAR(period)`
- [x] `K1`, `K2`, `PERDAS` injectable, defaulting to Horária
- [x] Separate `total_daily_cost()` adding K₃ + TAR potência + VAT, for reporting

**Verification:**
- [x] Reconstruct the energy price for a known month and compare against the invoice (1% tolerance)
- [x] `pytest tests/test_prices.py`

**Dependencies:** Task 1
**Files:** `core/prices.py`, `tests/test_prices.py`
**Scope:** S

---

### Task 3: Historical OMIE ingestion

**Description:** Load the quarter-hourly OMIE series (or hourly, if that is what exists) into a tabular form usable by the backtest. Resolves Open Question #1.

**Acceptance criteria:**
- [x] Loads 12 months into a timestamp-indexed structure
- [x] Documents the real granularity available (hourly through Sep-2025, quarter-hourly from Oct-2025)
- [x] Validates against the known monthly period averages (see `docs/tariff-reference.md`)

**Verification:**
- [x] Per-period averages computed from raw data match the known MA30 series (pinned deviations in `tests/test_omie_validation.py`)

**Dependencies:** Task 1
**Files:** `backtest/load_omie.py`, `backtest/data/`
**Scope:** S

---

### Checkpoint A

- [x] `pytest` green
- [x] Calendar validated against the real invoice: 8.7% of consumption in ponta for the June period
- [x] **Human review before proceeding** — done; findings and doc edits (1–9) recorded in `docs/findings.md`

---

### Task 4: Greedy optimiser

**Description:** `solve(prices, load, solar, params) -> Plan`. Implements the algorithm in spec §7, honouring C-1..C-8.

**Acceptance criteria:**
- [x] Returns a 96-interval plan respecting C-1..C-7
- [x] `WEAR_COST` parameterisable; at zero it cycles more
- [x] `CAP_USABLE` parameterisable — this is how the second unit gets tested
- [x] Also returns the forecast saving, for comparison

**Verification:**
- [x] Property test: 1000 random days, no constraint violations
- [x] Property test: saving ≥ the fixed schedule's saving, always
- [x] Degenerate case: flat prices → no cycling

**Dependencies:** Tasks 1, 2
**Files:** `core/optimiser.py`, `tests/test_optimiser.py`
**Scope:** M

---

### Task 5: Static baseline

**Description:** `static_plan(day, params) -> Plan` — the fixed seasonal schedule. Charge in vazio Nov–Apr, midday May–Oct; discharge in ponta. Serves as both the comparison reference and the production fallback.

**Acceptance criteria:**
- [x] Produces a valid plan without consulting prices
- [x] Respects C-1..C-7
- [x] Switches the charging window by season

**Verification:**
- [x] Annual backtest (Task 6) yields **€196.10/yr incl. VAT** — supersedes the MA30-derived ~€267 estimate, which assumed full ponta coverage (winter weekdays demand 5.2 kWh vs 3.46 deliverable) and did not net wear. See `docs/findings.md`.

**Dependencies:** Tasks 1, 4
**Files:** `core/static_schedule.py`, `tests/test_static.py`
**Scope:** S

---

### Task 6: Backtest harness

**Description:** `backtest/run.py` — runs a strategy over N months of data and produces a comparative report.

**Acceptance criteria:**
- [x] Accepts strategy, capacity and tariff parameters as arguments
- [x] Produces: annual cost, saving vs. no-cycling, saving vs. static, cycles/year, throughput
- [x] Writes results to CSV for inspection

**Verification:**
- [x] `python backtest/run.py --strategy static` yields €196.10/yr incl. VAT (measured; supersedes the ~€267 MA30 estimate — see `docs/findings.md`)
- [x] Runs 12 months in <10 s (measured ~0.3–1.3 s per strategy)
- [x] Handles negative OMIE prices without special-casing — no `abs()`, no `max(0, price)` (`docs/findings.md` §Negative OMIE prices)

**Dependencies:** Tasks 3, 4, 5
**Files:** `backtest/run.py`, `backtest/report.py`
**Scope:** M

---

### Checkpoint B — the four answers

**Answered 2026-08-05** — numbers and the owner's decisions are in `docs/findings.md` (§Checkpoint B decisions): Phase 1 GO; cheias cycling capped via planning-wear margin; reference figures updated to measured; second unit not purchased.

Run the backtest in the following configurations and record results in `docs/findings.md`:

| Question | Command | Decision criterion |
|---|---|---|
| Is dynamic worth it? | `--strategy greedy` vs `--strategy static` | Gain ≥ €30/year → proceed to Phase 1 |
| Is a second unit worth it? | `--cap 5` vs `--cap 10` | Gain ≥ €100/year → reconsider the purchase |
| Horária or Média? | `--k1 1.08 --hourly` vs `--k1 1.10 --monthly` | Confirms the tariff choice |
| Switch tariff now? | `--tariff horaria-tri` vs `--tariff simples-15` | Gain ≥ €100/yr → switch when the 35% expires |

**Scope Checkpoint B to the 11 quarter-hourly months (Oct 2025 onward).**
Sep-2025 is hourly-only, and intra-period selection is precisely what the
dynamic optimiser is being measured on — including it dilutes the result.
Report Sep-2025 separately as a data point on what resolution is worth.

**If dynamic yields <€30/year: stop here.** Run the static schedule manually or with a trivial YAML automation. Phase 1 onward stops being justified.

---

## PHASE 1 — HA shell, static execution

**Goal:** actuate the battery safely, with the simplest possible plan.

### Task 7: Driver

**Description:** Thin layer over the `marstek_venus_modbus` integration. Interface: `set_mode()`, `set_charge_power()`, `set_discharge_power()`, `read_soc()` — the device has separate charge/discharge power registers (42020/42021), verified from the integration source. **Never direct Modbus** (ADR-0004).

**Acceptance criteria:**
- [x] Abstract interface + real implementation + fake implementation for tests
- [x] All writes via `hass.services.async_call`
- [x] 3 consecutive failures → raises a handleable exception (`DriverUnavailableError`)

**Verification:**
- [x] Fake-driver tests verify the call sequence
- [ ] Manual test: force 500 W charge and confirm on the device — **blocked: battery not yet arrived**. Mode labels (`charge`/`discharge`/`standby`) verified against the integration source meanwhile; entities ship disabled by default and must be enabled in HA first

**Dependencies:** None (parallelisable with Phase 0)
**Files:** `custom_components/battery_opt/driver.py`, `tests/test_driver.py`
**Scope:** S

---

### Task 8: Integration skeleton

**Description:** `manifest.json`, `config_flow.py`, `__init__.py`, `coordinator.py`. The config flow collects: battery entities, price entity, capacity, reserve floor, `WEAR_COST`.

**Acceptance criteria:**
- [x] Installable via HACS from the repository (hacs.json/manifest/README; blueprint template removed)
- [x] Config flow works; options editable afterwards (battery entities optional as a group — planning-only mode until the battery arrives)
- [x] `DataUpdateCoordinator` performs no blocking work on the event loop

**Verification:**
- [x] `pytest` with `pytest-homeassistant-custom-component` (0.13.340, HA 2026.6.4)
- [x] Clean install on a test HA instance (`scripts/develop`, 2026-08-05: zero battery_opt errors at boot)

**Dependencies:** Task 7
**Files:** `manifest.json`, `config_flow.py`, `__init__.py`, `coordinator.py`, `strings.json`
**Scope:** M

---

### Task 9: Executor + sensors

**Description:** 15-minute trigger applying the current interval's setpoint. Plan, savings and health sensors.

**Acceptance criteria:**
- [x] Applies the plan interval by interval (setpoints floored DOWN to the 50 W device step — up would export on C-1 or breach the C-3 margin)
- [x] `binary_sensor.battery_opt_healthy` reflects real state
- [x] Never actuates when `healthy` is false
- [x] Validates the plan against C-1..C-7 before each actuation

**Verification:**
- [ ] 48 h in production on the static plan: zero export, SoC within bounds — **blocked: battery not yet arrived**
- [ ] Power off the battery → `healthy` goes off within 45 min — **blocked: battery not yet arrived**

**Dependencies:** Task 8
**Files:** `sensor.py`, `binary_sensor.py`, `executor.py`
**Scope:** M

---

### Delivered ahead of plan: planning-only mode (2026-08-05)

The battery had not arrived, so the shell runs without it: leave the four
Marstek entities empty in the config flow and the integration computes the
day's advisory plan from the OMIE price sensor — capped greedy (plan-wear
0.0467 per the Checkpoint B decision, savings booked at the true wear),
virtual battery from the reserve floor, nothing actuates. Sensors
`battery_opt_plan`, `battery_opt_forecast_savings` and
`battery_opt_vs_static` are live from real prices. This pulls forward the
safe parts of Task 10 (basic price reading) and Task 12 (dry-run +
`vs_static` sensor); their remaining criteria stand. It also settled the
production half of open question #1 from the `hass_omie` source:
quarter-hourly (`docs/spec.md` §12).

Added alongside (2026-08-05): `sensor.battery_opt_current_price` — the
delivered price for the current quarter-hour per the EDP Indexada
formula, declared like core OMIE's price sensor (€/kWh, state_class
measurement) so it plugs into the Energy dashboard as the grid price
entity; the full day vector and TAR period ride in the attributes.

---

### Checkpoint C

**Blocked until the battery arrives and Tasks 7/9 manual verifications run.**

- [ ] 2 weeks in production on the static plan
- [ ] Zero export recorded
- [ ] SoC never below 27%
- [ ] Ponta coverage ≥95%
- [ ] **Human review before enabling dynamic**

---

## PHASE 2 — Dynamic

### Task 10: Production price ingestion

**Description:** Read the `omie` integration entity, build the 96-price vector, with retry and fallback.

*Partially delivered early (planning-only mode, 2026-08-05): `prices_source.py` reads the entity and builds the delivered-price vector, verified against the `hass_omie` attribute shape. The 13:45 trigger/retry schedule, the `healthy=off`+static fallback and price archiving remain.*

**Delivered 2026-08-06, planning-only** (overnight session, Task A): the
remaining three criteria landed together. `__init__.py` registers four
`async_track_time_change` triggers (13:45, 14:15, 15:00, 16:00 Lisbon
local) that each force `coordinator.async_request_refresh()` — simpler
than conditional retry-only-on-failure logic, and harmless when a
fetch already succeeded (`test_coordinator.py`). When no trustworthy
price vector exists (missing OMIE, or defensively an invalid dynamic
plan), the coordinator now publishes the static seasonal schedule
instead and marks it via a `fallback: "static"` attribute on
`sensor.battery_opt_plan` (`coordinator.py::_static_fallback`,
`sensor.py`). Prices archive to
`hass.config.path("battery_opt/prices")/YYYY-MM-DD.json` on every
successful full-day build via the new `archive.py`, keyed so a later
un-padded write naturally overwrites a padded one at the same path.
`prices_source.py` gained `DaySeries`/`day_series_from_service` to
carry the raw OMIE EUR/MWh points the archive needs, alongside the
delivered vector; `day_price_vector_from_service` is now a thin
wrapper over it and is unchanged for existing callers.

**Acceptance criteria:**
- [x] Trigger at 13:45; retry at 14:15, 15:00, 16:00
- [x] No prices after the final retry → `healthy=off` + static plan
- [x] Prices archived to `/config/battery_opt/prices/`

**Verification:**
- [x] Simulate source failure and confirm the fallback

**Dependencies:** Tasks 2, 9
**Files:** `coordinator.py`, `core/prices.py`, `archive.py` (new), `prices_source.py`, `__init__.py`, `sensor.py`
**Scope:** S

---

### Task 11: Load forecast

**Description:** Median of the last 4 same-weekday occurrences, per interval. Fallback: flat 1.04 kW.

**Delivered 2026-08-06, planning-only** (overnight session, Task B):
`core/forecast.py::forecast_load` is the pure per-slot median-of-4
function (HA-free, exhaustively unit-tested in `tests/test_forecast.py`
— fewer-than-4-days flat fallback, per-slot fallback on a missing
sample, most-recent-4-only, solar subtraction and its floor at zero).
`load_history.py` is the HA-side adapter that feeds it from recorder
long-term statistics.

**Deliberate resolution deviation from this task's wording:** recorder
long-term statistics are HOURLY, not quarter-hourly — short-term
5-minute stats only survive ~10 days, too short for a 4-same-weekday
(4-week) lookback. `load_history.py` expands each hour into its 4
identical quarters. True quarter resolution arrives later from our
own load archive (`archive.py`'s new `battery_opt/load/` path,
accumulated at day close) once enough days have accumulated —
tracked, not solved, by this delivery.

The coordinator uses the forecast as the advisory plan's load input
whenever `CONF_LOAD_SENSOR` is configured (flat `BASE_LOAD_W`
otherwise); the meter selector is optional and existing entries reload
unchanged without it. Day close at 00:05 archives yesterday's observed
load and computes `sensor.battery_opt_load_mae` (W, unknown until a
meter exists and one day has closed), persisted across restarts via
`homeassistant.helpers.storage.Store`.

**Acceptance criteria:**
- [x] Uses `recorder` history
- [x] With <4 weeks of data, uses the constant
- [x] Subtracts the solar forecast before returning

**Verification:**
- [x] Mean absolute error vs. actual exposed as a sensor, for monitoring (`sensor.battery_opt_load_mae`)
- [ ] Real-world accuracy check — needs several weeks of accumulated same-weekday history in production; nothing to measure yet the night this landed

**Dependencies:** Task 10
**Files:** `core/forecast.py`, `load_history.py`, `coordinator.py`, `sensor.py`, `config_flow.py`, `strings.json`, `translations/en.json`, `archive.py`, `__init__.py`
**Scope:** M

---

### Task 12: Enable the optimiser

**Description:** Replace the static plan with the greedy, keeping static as fallback.

*Partially delivered early (planning-only mode, 2026-08-05): the advisory capped-greedy plan and `sensor.battery_opt_vs_static` already run as a permanent dry-run. What remains is exactly the actuation swap — executor running the greedy with static fallback — gated on Checkpoint C.*

**Acceptance criteria:**
- [ ] `sensor.battery_opt_vs_static` publishes the daily forecast gain
- [ ] If the greedy fails or produces an invalid plan, fall back to static
- [ ] `dry_run` mode that computes and publishes but does not actuate

**Verification:**
- [ ] 1 week in `dry_run`, comparing the planned schedule against what would have been optimal
- [ ] 3 months in production: realised saving > static

**Dependencies:** Tasks 6, 11
**Files:** `coordinator.py`, `core/optimiser.py`
**Scope:** S

---

### Task 13: Reporting and reconciliation

**Description:** Ex-post realised saving, and monthly comparison against the invoice.

**Acceptance criteria:**
- [ ] `sensor.battery_opt_realised_savings` computed from actual SoC and prices
- [ ] Monthly report: forecast vs. invoiced, with deviation
- [ ] Deviation >10% raises a notification

**Verification:**
- [ ] First invoice month reconciled manually

**Dependencies:** Task 12
**Files:** `core/reporting.py`, `sensor.py`
**Scope:** M

---

### Checkpoint D

- [ ] 3 months in production
- [ ] Realised saving > static, confirmed by invoice
- [ ] No invariant violations recorded

---

## PHASE 3 — Extensions (only if justified)

| Task | Condition to proceed |
|---|---|
| Integrate solar forecast into the model | Panel installed |
| Evaluate EMHASS / LP | Backtest shows >€10/year of headroom vs. optimal |
| ERSE 2027 calendar | ERSE publishes the new hours |
| Second unit | Checkpoint B shows gain ≥€100/year |
| Publish `pt-erse-tariff` as a package | Third-party interest |
| ~~Actual-cost sensor from the meter~~ | **Delivered 2026-08-06** (owner picked the grid-import energy sensor via decision 1's config addition) |

**Actual-cost sensor (requested 2026-08-05, delivered 2026-08-06,
overnight session Task C):** `sensor.battery_opt_cost_today` (EUR,
`state_class` TOTAL, `last_reset` = local midnight — not
`total_increasing`, since a meter reset can otherwise appear to
decrease the value that state class promises never decreases) fed by
the household meter's grid-import energy sensor
(`CONF_GRID_ENERGY_SENSOR`, added to the config/options flow alongside
`CONF_LOAD_SENSOR` in Task B): each state-change delta × the delivered
price at that instant (a negative delta from a meter reset counts as
0), **plus the per-day fixed terms** (K3 €0.1171/day + TAR potência
€0.2291/day) — which a €/kWh price sensor can never carry — added once
per day. Implementation: `cost.py` (`CostToday` pure accumulator,
`CostTracker` HA-side state-change wiring with its own
`homeassistant.helpers.storage.Store` so the day's accumulation
survives restarts) plus `CostTodaySensor` in `sensor.py`. VAT is
deliberately left out: the reduced rate on the first 200 kWh/30 days
makes it a billing-window computation, not a per-quarter one — still
parked for Task 13's invoice reconciliation, which needs that logic
anyway. Unavailable without a configured meter.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Wrong calendar** | **High** — discharges into cheias for months without any signal | Mandatory test table; validation against the real invoice at Checkpoint A |
| OMIE only hourly, not quarter-hourly | Medium — loses resolution | Task 3 resolves it early; quantify before investing in Phase 2 |
| Variable losses not modelled | Low — a few € of bias | Record deviation against the invoice; correct if material |
| Dynamic gain < €30/year | Medium — project not justified | **This is exactly what Checkpoint B exists for** |
| ERSE 2027 reform changes the economics | Medium | Versioned calendar (ADR-0005); expected to be favourable |
| Modbus conflict with another integration | High — loses control | ADR-0004; never open our own connection |
| Solar cannibalises the battery | Low — already modelled | Reconcile after installation |

---

## Parallelisation

- **Parallelisable:** Task 7 (driver) alongside all of Phase 0 — no dependency between them
- **Strictly sequential:** Task 1 → 2 → 4 → 6; Task 8 → 9
- **Needs coordination:** Tasks 11 and 12 share the load-vector contract — define the signature first

---

## Open Questions

See `docs/spec.md` §12. The ones that block work:

- **#1 (OMIE granularity)** blocks Task 3 → resolve first
- **#3 (zero-export internal or external)** blocks Task 7 → confirm on the device
