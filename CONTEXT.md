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
| **Plan** | Vector of 96 setpoints (mode + power) for the next day |
| **Zero-export** | Mode in which discharge never exceeds instantaneous consumption. Nothing is injected into the grid |
| **Reserve floor** | Minimum SoC that arbitrage never consumes, held for outages |
| **Wear cost** | Marginal cost, in €/kWh, of battery ageing per unit of energy processed |
| **Static baseline** | Fixed seasonal schedule, no optimisation. The reference the optimiser must beat |

Portuguese period names (`ponta`, `cheias`, `vazio`) are kept untranslated throughout the code. They are regulatory terms with no clean English equivalent, and translating them invites confusion when cross-checking against ERSE or EDP documents.

---

## Constants

```python
# --- Tariff: EDP Eletricidade Indexada Horária DD+FE ---
K1      = 1.08          # Indexada Horária. Indexada Média uses 1.10
K2      = 0.0185        # EUR/kWh
K3      = 0.1171        # EUR/day - fixed term, not part of the optimisation
PERDAS  = 0.164         # loss coefficient; on Horária this varies per quarter-hour

# --- TAR energia, ERSE 2026, BTN <=20.7 kVA, tri-horária (EUR/kWh) ---
TAR_PONTA  = 0.2452
TAR_CHEIAS = 0.0412
TAR_VAZIO  = 0.0158
TAR_POTENCIA = 0.2291   # EUR/day @ 4.6 kVA - constant, outside the optimisation

VAT = 1.23              # uniform multiplier; reporting only

# --- Battery: Marstek Venus E 3.0 ---
CAP_NOMINAL = 5.12      # kWh
CAP_USABLE  = 5.00      # kWh
CAP_MIN     = 1.35      # kWh (27% - reserve floor, zero arbitrage cost)
P_CHG_MAX   = 2000      # W (limited by margin against contracted power)
P_DIS_MAX   = 2500      # W
ETA_RT      = 0.90      # round-trip efficiency
RATED_CYCLES = 6000
PRICE_PAID  = 1400      # EUR

# --- Installation ---
CONTRACTED_VA = 4600    # VA
P_USABLE      = 4400    # W (200 W margin)
BASE_LOAD     = 1040    # W - flat 24/7 load (AC + homelab)
DAILY_KWH     = 24.6

# --- Solar (if installed) ---
PANEL_WP      = 460
ANNUAL_YIELD  = 501     # kWh/year, estimated
```

### Wear cost

```
WEAR_COST_MAX = PRICE_PAID / (RATED_CYCLES * CAP_USABLE) = 1400 / 30000 = 0.0467 EUR/kWh
WEAR_COST     = 0.020 EUR/kWh   # default; test sensitivity between 0 and 0.0467
```

Without this term the optimiser cycles for one-cent spreads and consumes battery life for nothing.

---

## Actual consumption profile

Measured on the EDP invoice for 2 Jun – 1 Jul 2026 (644 kWh over 30 days) and validated against the hourly model:

| | Measured | Model (semanal, summer) |
|---|---|---|
| Ponta | 56 kWh (8.7%) | 57.5 kWh (8.93%) |
| Vazio | 265 kWh (41.1%) | 41.7% |

2.6% deviation — confirms the load is **effectively flat** and the meter is on the **weekly cycle**.

Ponta consumption at ~750 kWh/month:

| Season | Ponta hours/day | kWh in ponta/day |
|---|---|---|
| Summer (7 months) | 2.14 | **2.20** |
| Winter (5 months) | 3.57 | **3.67** |

**One 5 kWh unit covers 100% of ponta in both seasons.** Capacity is not the binding constraint — the length of the ponta window and the household load are.

---

## Invariants

Violating any of these is a bug, not a configuration choice.

1. **Never export.** `discharge[i] <= net_load[i]`, always.
2. **Never exceed contracted power.** `battery_charge[i] + house_load[i] <= P_USABLE`.
3. **Never drop below the reserve floor.** `SoC[i] >= CAP_MIN`.
4. **Never charge and discharge in the same interval.**
5. **Never open a second Modbus connection.** The device accepts one at a time, and the `marstek_venus_modbus` integration already owns it.
6. **The tariff calendar is versioned by effective date.** Never hardcoded to the current year.
7. **`core/` imports nothing from `homeassistant`.** This is what makes testing and backtesting outside HA possible.

---

## Reference figures

Any change to the optimiser is measured against these:

| Scenario | Annual saving (incl. VAT) |
|---|---|
| No cycling | €0 |
| **Static seasonal schedule** | **~€267** |
| Dynamic (target) | €300–345 |
| Theoretical ceiling | Bounded by household load, not battery capacity |

Battery payback at €1,400: ~5.4 years. Cycle life: ~29 years (the 10-year warranty and calendar ageing bind, not cycles).

**Second unit: ~€45/year, ~30-year payback.** Under evaluation, but the binding constraint is the household load (1.04 kW), not capacity. Adding storage does not create hours in the day.

---

### Current tariff state

| Period | Tariff | Energy €/kWh | Battery arbitrage |
|---|---|---|---|
| Now → month 3 | Simples, 35% discount | 0.1086 | **None** — simples has no period differentiation |
| Month 3 → Mar 2027 | Simples, 15% discount | 0.1420 | None |
| Target | Indexada Horária, tri-horária, weekly | variable | ~€267/yr static, €300–345 dynamic |

The battery earns nothing until the tariff option changes. Phase 0 exists
because of this, not in spite of it.

---

## Known traps

- **The calendar is the most likely source of error in the entire system.** Weekends, daylight-saving changes, and the 09:15 / 12:15 / 18:30 boundaries. A mistake here discharges into cheias for six months without raising any signal.
- **Sunday is entirely vazio and Saturday has no ponta** under the weekly cycle. This is why ~20% of solar production lands in vazio.
- **Summer ponta (09:15–12:15) coincides with the Iberian solar peak**, when OMIE is at its lowest. The ERSE reform expected around January 2027 moves ponta to the end of the day while preserving daily durations — this should **improve** arbitrage.
- **Solar cannibalises the battery.** A solar kWh during ponta displaces battery discharge, not grid import. It is worth ~€0.129/kWh, not €0.329. Do not add the two savings together.
- **VAT and fixed terms do not change the optimum.** They are uniform constants. Include them in reporting only.

---

## Sources

- EDP invoice and standardised offer sheets "Indexada Média DD + FE" and "Indexada Horária DD + FE", effective 01/01/2026
- ERSE 2026 tariffs, BTN ≤20.7 kVA
- OMIE MA30 series by period (Sep 2025 – Aug 2026), daily and weekly cycles
- Marstek Venus E 3.0 manual
