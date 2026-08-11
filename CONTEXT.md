# CONTEXT

Domain language, constants and invariants for this project. Read before changing any code.

---

## What this project is

A Home Assistant integration that plans and executes charging and discharging of a **Marstek Venus E 3.0** home battery, with the goal of **minimising annual electricity cost** through arbitrage between tariff periods.

**What is being arbitraged is not the electricity market — it is the regulated network tariff (TAR).**

This is the single most important fact in the domain. The ponta TAR (€0.2452/kWh) is **15.5×** the vazio TAR (€0.0158/kWh) and is set annually by ERSE. The market component (OMIE) is volatile and, in 9 of the 12 months analysed, works *against* us — in June 2026 energy during ponta cost less than a tenth of energy during vazio. Arbitrage remains profitable because the TAR dominates.

Practical consequence: **any strategy that ignores the TAR and follows OMIE prices is wrong.**

---

## Glossary

| Term | Meaning |
|---|---|
| **TAR** | *Tarifa de Acesso às Redes* — regulated network access tariff, set by ERSE, revised annually. Two components: **TAR energia** (€/kWh, varies by time period) and **TAR potência** (€/day, varies only with contracted power) |
| **Period** | `ponta` (peak), `cheias` (shoulder) or `vazio` (off-peak). Determines the applicable TAR energia |
| **Cycle** | `diário` (daily) or `semanal` (weekly). Defines how hours map to periods. **We use semanal** |
| **Tariff option** | `simples`, `bi-horária` or `tri-horária`. **We use tri-horária** |
| **OMIE** | Iberian electricity market. Next-day prices published ~13:30 |
| **Indexada Média** | EDP tariff billing at the monthly average OMIE price per period. K₁ = 1.10 |
| **Indexada Horária** | EDP tariff billing at the quarter-hourly OMIE price. K₁ = 1.08. **This is the target** |
| **Interval** | A 15-minute slot. The day has 96 |
| **Plan** | The day's 96 quarter-hour battery states (CHARGE/HOLD/DISCHARGE). The optimiser still models power and energy internally (C-3..C-6), but actuation reads states only (ADR-0007) |
| **Zero-export** | Mode in which discharge never exceeds instantaneous consumption. Nothing is injected into the grid |
| **Reserve floor** | Minimum SoC that arbitrage never plans into, held for outages. A planning constraint (C-4); run-time enforcement is the battery's (ADR-0008) |
| **Wear cost** | Marginal cost, in €/kWh, of battery ageing per unit of energy processed |
| **Static baseline** | Fixed seasonal schedule, no optimisation. The reference the optimiser must beat |
| **Battery state** | Planner-level state at any instant: `CHARGE`, `HOLD` or `DISCHARGE`. Each state uses a different control mechanism (ADR-0006) |
| **External control** | The battery's Modbus command mode: HA forces charging, standby or discharging directly. Used for CHARGE and HOLD |
| **Anti-feed** | The firmware's automatic zero-export mode: the battery tracks the paired meter and discharges to match house load, never exporting. The only mechanism with native zero-export tracking; used for DISCHARGE |
| **Watchdog** | Firmware timer reported by the community to drop external control ~15 s after Modbus traffic stops. **Measured absent on this unit's firmware (2026-08-11):** force-charge survived a network cut for over 2 min. Re-test after every OTA |
| **Keepalive** | Whatever prevents the watchdog from firing — moot while the watchdog is absent (above); polling cadence is a data-freshness concern only |
| **Charge-power loop** | Fast control loop (ADR-0007) that, while in CHARGE, continuously sets the charge setpoint to the highest value keeping measured total grid import under the contracted ceiling. The plan carries states only; both run-time magnitudes are closed loops — discharge via anti-feed, charge via this |

Portuguese period names (`ponta`, `cheias`, `vazio`) are kept untranslated throughout the code. They are regulatory terms with no clean English equivalent, and translating them invites confusion when cross-checking against ERSE or EDP documents.

---

## Constants

```python
# --- Tariff: EDP Eletricidade Indexada Horária DD+FE ---
K1 = 1.08  # Indexada Horária. Indexada Média uses 1.10
K2 = 0.0185  # EUR/kWh
K3 = 0.1171  # EUR/day - fixed term, not part of the optimisation
PERDAS = 0.164  # loss coefficient; on Horária this varies per quarter-hour

# --- TAR energia, ERSE 2026, BTN <=20.7 kVA, tri-horária (EUR/kWh) ---
TAR_PONTA = 0.2452
TAR_CHEIAS = 0.0412
TAR_VAZIO = 0.0158
TAR_POTENCIA = 0.2291  # EUR/day @ 4.6 kVA - constant, outside the optimisation

VAT = 1.23  # uniform multiplier; reporting only

# --- Battery: Marstek Venus E 3.0 ---
CAP_NOMINAL = 5.12  # kWh
CAP_USABLE = 5.00  # kWh
CAP_MIN = 1.35  # kWh (27% - reserve floor, zero arbitrage cost)
P_CHG_MAX = 2000  # W - historical static ceiling; SUPERSEDED (Task 15,
#   2026-08-07, ADR-0007): the charge-power loop drives the setpoint up
#   to the device's 2500 W against the MEASURED import, keeping the
#   200 W margin. Production planning C-3 passes the 2500 W device
#   limit; the backtest keeps 2000 (the configuration the Checkpoint B
#   reference figures were measured under). 2000 remains the loop's
#   fail-safe fallback when its sensors are unavailable.
P_DIS_MAX = 2500  # W
ETA_RT = 0.90  # round-trip efficiency
RATED_CYCLES = 6000
PRICE_PAID = 1400  # EUR

# --- Installation ---
CONTRACTED_VA = 4600  # VA
P_USABLE = 4400  # W (200 W margin)
BASE_LOAD = 1040  # W - flat 24/7 load (AC + homelab)
DAILY_KWH = 24.6

# --- Solar (if installed) ---
PANEL_WP = 460
ANNUAL_YIELD = 501  # kWh/year, estimated
```

### Wear cost

```
WEAR_COST_MAX = PRICE_PAID / (RATED_CYCLES * CAP_USABLE) = 1400 / 30000 = 0.0467 EUR/kWh
WEAR_COST     = 0.020 EUR/kWh   # default; test sensitivity between 0 and 0.0467
```

Without this term the optimiser cycles for one-cent spreads and consumes battery life for nothing.

**Basis note (2026-08-07):** C-8 and the evaluator book `WEAR_COST`
per **meter-side** kWh discharged; `WEAR_COST_MAX` above is derived
per **battery-side** kWh (30,000 kWh lifetime through the cells). The
~5% (1/√η) mismatch is well inside the tested 0–0.0467 sensitivity
band (cycling volume responds, the decision does not — findings);
if aligning ever matters, divide the meter-side rate by √η.

---

## Actual consumption profile

Measured on the EDP invoice for 2 Jun – 1 Jul 2026 (644 kWh over 30 days) and validated against the hourly model:

| | Measured | Model (semanal, summer) |
|---|---|---|
| Ponta | 56 kWh (8.7%) | 57.5 kWh (8.93%) |
| Vazio | 265 kWh (41.1%) | 41.7% |

2.6% deviation — confirms the load is **effectively flat** and the meter is on the **weekly cycle**.

Ponta consumption at ~750 kWh/month (≈ DAILY_KWH × 30.4; the June
invoice measured 644):

| Season | Ponta hours/day | kWh in ponta/day |
|---|---|---|
| Summer (7 months) | 2.14 | **2.20** |
| Winter (5 months) | 3.57 | **3.67** |

**Coverage (corrected at Checkpoint B):** the winter row is a 7-day
average that hides the weekday reality — ponta exists only Mon–Fri, at
5 h/weekday = **5.2 kWh**, against **3.46 kWh deliverable** above the
reserve floor (3.65 kWh × √η). One 5 kWh unit covers all of summer
ponta but only **~67% of winter weekday ponta**; capacity binds on
winter weekdays (which is exactly what the second-unit measurement
priced at +€111/yr — not bought). Elsewhere the window length and the
household load bind. The original "one unit covers 100% in both
seasons" claim was wrong.

**Figure hygiene (2026-08-07):** three daily-consumption figures
coexist — the June invoice (644 kWh/30 d = 21.5 kWh/day), `DAILY_KWH
= 24.6`, and `BASE_LOAD` 1040 W ⇒ 24.96 kWh/day, which is what the
backtest simulates. The backtest therefore runs ~16% above the
measured invoice; savings scale roughly with load, so the measured-€
figures are mildly optimistic until a real load meter feeds Task 11
(see also `docs/findings.md` §Measured ponta share).

---

## Invariants

Violating any of these is a bug, not a configuration choice.

1. **Never export.** `discharge[i] <= net_load[i]`, always.
2. **Never exceed contracted power.** `battery_charge[i] + house_load[i] <= P_USABLE`.
3. **Never plan below the reserve floor.** `SoC[i] >= CAP_MIN` in every plan's modelled trajectory (C-4). Run-time floor enforcement is delegated to the battery (ADR-0008, owner 2026-08-07): the firmware discharge cutoff where the register exists, the device's own minimum otherwise — the integration reads no SoC.
4. **Never charge and discharge in the same interval.**
5. **Never open a second Modbus connection.** The device accepts one at a time, and the `marstek_venus_modbus` integration already owns it.
6. **The tariff calendar is versioned by effective date.** Never hardcoded to the current year.
7. **`core/` imports nothing from `homeassistant`.** This is what makes testing and backtesting outside HA possible.

---

## Reference figures

Any change to the optimiser is measured against these — **measured by the
Task 6 backtest** (11 quarter-hourly months of real OMIE data, net of
€0.020/kWh wear; method and superseded MA30 estimates in `docs/findings.md`):

| Scenario | Annual saving (incl. VAT) |
|---|---|
| No cycling | €0 |
| **Static seasonal schedule** | **€196** (was est. ~€267: full-coverage assumption, wear not netted) |
| **Dynamic (greedy, uncapped)** | **€327** (est. band was €300–345) |
| Theoretical ceiling | Bounded by household load, not battery capacity |

Battery payback at €1,400: ~7.1 years static, ~4.3 dynamic. Cycle life at the uncapped greedy's 417 cycles/yr: ~14 years — the 10-year warranty still binds first; production runs capped (see `docs/findings.md` §Checkpoint B decisions).

**Second unit: measured +€111/year, ~12.7-year payback. Decision: not purchased** (Checkpoint B). The binding constraint is the household load (1.04 kW), not capacity — adding storage does not create hours in the day.

---

### Current tariff state

| Period | Tariff | Energy €/kWh | Battery arbitrage |
|---|---|---|---|
| Now → month 3 | Simples, 35% discount | 0.1086 | **None** — simples has no period differentiation |
| Month 3 → Mar 2027 | Simples, 15% discount | 0.1420 | None |
| Target | Indexada Horária, tri-horária, weekly | variable | €196/yr static, €327 dynamic (measured, Task 6) |

The battery earns nothing until the tariff option changes. Phase 0 exists
because of this, not in spite of it.

**Decision at month 3:** staying on the 15% fixed tariff costs ~€1,437/yr;
moving to Indexada Horária tri-horária with the battery costs ~€1,254/yr —
a €183/yr advantage for switching. That margin absorbs a ~16 €/MWh rise in
average OMIE before it becomes neutral. During the 35% promotion the fixed
tariff wins by ~€118/yr, so do not switch early. (The Task 6 backtest
measures the switching advantage at €263/yr on real data, energy component
only — see `docs/findings.md`. **VAT basis note:** the ~€1,437/~€1,254
figures here are MA30-era estimates excl. VAT; findings' measured billed
counterparts — €1,746.34 vs €1,482.98 — include VAT, hence the different
magnitudes.)

---

## Known traps

- **The calendar is the most likely source of error in the entire system.** Weekends, daylight-saving changes, and the 09:15 / 12:15 / 18:30 boundaries. A mistake here discharges into cheias for six months without raising any signal.
- **Sunday is entirely vazio and Saturday has no ponta** under the weekly cycle. This is why ~20% of solar production lands in vazio.
- **Summer ponta (09:15–12:15) coincides with the Iberian solar peak**, when OMIE is at its lowest. The ERSE reform expected around January 2027 moves ponta to the end of the day while preserving daily durations — this should **improve** arbitrage.
- **Solar cannibalises the battery.** A solar kWh during ponta displaces battery discharge, not grid import. It is worth ~€0.129/kWh, not €0.329. Do not add the two savings together.
- **VAT and fixed terms do not change the optimum.** They are uniform constants. Include them in reporting only.
- **OMIE prices can be negative.** Nothing may assume `price >= 0` — no `abs()`, no `max(0, price)`; property-test generators draw from roughly −50 to +300 €/MWh. The parser rejects values outside the SDAC clearing limits (~−500 to +4000 €/MWh) as errors. Delivered price stays positive in practice (K₂ + TAR are added regardless of sign) and a negative OMIE can never invert the ponta/vazio ranking. See `docs/findings.md` §Negative OMIE prices.
- **There is no force-mode watchdog on this unit's firmware (measured 2026-08-11).** The community-reported ~15 s self-stop never fired in a network-cut test: a dead integration leaves force-charge running until the 42011 charge-to-SoC backstop target — the backstop is **load-bearing**, so `charge_to_soc` is effectively required in active mode. Undocumented firmware behaviour either way: re-run the on-device checklist (spec §8) after every firmware OTA before trusting a single transition.
- **Entering force mode flips the work mode to manual** (confirmed on-device): anti-feed must be re-asserted on every transition into DISCHARGE. Releasing external control *appeared* to restore anti-feed on the current firmware — assume nothing; the re-assert stays. Integration death during HOLD leaves the battery in the commanded external-control stop (no watchdog clears it), so HOLD survives the integration dying.
- **A floor-seeded summer static day can never discharge.** Summer ponta (09:15–12:15) precedes the midday charge window (13:00–17:00), so a single-day plan starting at the reserve floor has nothing to serve it with — the battery would charge once and sit full all summer, delivering 0% ponta coverage. Production seeds each day from the previous weekday's PLANNED end SoC (`chained_start_soc`, spec §8) — the model rolled forward, never a SoC readback (ADR-0008 stands). Winter is naturally immune: every winter weekday ends drained, so the chained seed equals the floor there.

---

## Sources

- EDP invoice and standardised offer sheets "Indexada Média DD + FE" and "Indexada Horária DD + FE", effective 01/01/2026
- ERSE 2026 tariffs, BTN ≤20.7 kVA
- OMIE MA30 series by period (Sep 2025 – Aug 2026), daily and weekly cycles
- Marstek Venus E 3.0 manual
