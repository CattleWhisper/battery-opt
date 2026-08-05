# Tariff Reference

Reference data for `core/calendar.py` and `core/prices.py`. Everything here is verifiable against external sources — do not invent values.

Period names (`ponta`, `cheias`, `vazio`) are kept in Portuguese throughout: they are regulatory terms, and translating them breaks cross-referencing against ERSE and EDP documents.

---

## 1. TAR — ERSE 2026, BTN ≤20.7 kVA

### TAR energia (€/kWh)

| Option | Period | Value |
|---|---|---|
| Simples | — | 0.0607 |
| Bi-horária | Fora de vazio | 0.0835 |
| Bi-horária | Vazio | 0.0158 |
| **Tri-horária** | **Ponta** | **0.2452** |
| **Tri-horária** | **Cheias** | **0.0412** |
| **Tri-horária** | **Vazio** | **0.0158** |

**Ponta–vazio spread: €0.2294/kWh.** This is the economic basis of the entire project.

### TAR potência (€/day)

| Contracted power | Value |
|---|---|
| 4.60 kVA | 0.2291 |

Does not vary with the tariff option. Constant — outside the optimisation.

---

## 2. EDP Indexed Formula

```
Price = SUM_i [(P_OMIE,i x (1 + Perdas,i) x K1 + K2 + TAR_energia,i) x Consumption,i]
      + (K3 + TAR_potencia) x Days
```

| Parameter | Indexada Média | Indexada Horária |
|---|---|---|
| `P_OMIE` | Average price over the consumption period | Price in intervals ≥ the market adjustment frequency |
| `Perdas` | **16.4%** fixed | Variable, per published quarter-hourly profiles |
| **`K1`** | **1.10** | **1.08** |
| `K2` | €0.0185/kWh | €0.0185/kWh |
| `K3` | €0.1171/day | €0.1171/day |
| `Consumption` | Volume in the period | **Quarter-hourly** volume |

Source: EDP standardised offer sheets "Eletricidade Indexada média DD + FE" and "Eletricidade Indexada Horária DD + FE", effective 01/01/2026.

**Note:** Horária is cheaper on the market component (K₁ 1.08 vs 1.10) and offers tri-horária at 4.60 kVA. There is no margin penalty for choosing Horária.

---

## 3. Tri-horária calendar, weekly cycle

### Summer (last Sunday of March → last Sunday of October)

| Day | Vazio | Ponta | Cheias |
|---|---|---|---|
| Mon–Fri | 00:00–07:00 | **09:15–12:15** | 07:00–09:15, 12:15–24:00 |
| Saturday | 00:00–09:00, 14:00–20:00, 22:00–24:00 | — | 09:00–14:00, 20:00–22:00 |
| Sunday | 00:00–24:00 | — | — |

**Hours/week:** Vazio 76 · Ponta **15** · Cheias 77

### Winter (last Sunday of October → last Sunday of March)

| Day | Vazio | Ponta | Cheias |
|---|---|---|---|
| Mon–Fri | 00:00–07:00 | **09:30–12:00, 18:30–21:00** | 07:00–09:30, 12:00–18:30, 21:00–24:00 |
| Saturday | 00:00–09:30, 13:00–18:30, 22:00–24:00 | — | 09:30–13:00, 18:30–22:00 |
| Sunday | 00:00–24:00 | — | — |

**Hours/week:** Vazio 76 · Ponta **25** · Cheias 67

### Daily cycle (reference only — not used)

Ponta 4 h/day every day of the year; vazio 22:00–08:00; cheias the remainder.
Rejected: exposes 46% more consumption to ponta, costing ~€105/year more.

---

## 4. Mandatory test cases

```python
# Summer
assert period(datetime(2026, 7, 15, 9, 14)) == "cheias"  # 1 min before ponta
assert period(datetime(2026, 7, 15, 9, 15)) == "ponta"  # exact start
assert period(datetime(2026, 7, 15, 12, 14)) == "ponta"  # 1 min before end
assert period(datetime(2026, 7, 15, 12, 15)) == "cheias"  # exact end
assert period(datetime(2026, 7, 15, 19, 0)) == "cheias"  # summer has no evening ponta
assert period(datetime(2026, 7, 18, 10, 0)) == "cheias"  # Saturday: no ponta
assert period(datetime(2026, 7, 18, 15, 0)) == "vazio"  # Saturday afternoon
assert period(datetime(2026, 7, 19, 10, 0)) == "vazio"  # Sunday: all vazio

# Winter
assert period(datetime(2026, 1, 15, 9, 29)) == "cheias"
assert period(datetime(2026, 1, 15, 9, 30)) == "ponta"
assert period(datetime(2026, 1, 15, 12, 0)) == "cheias"
assert period(datetime(2026, 1, 15, 19, 0)) == "ponta"  # evening ponta
assert period(datetime(2026, 1, 15, 21, 0)) == "cheias"
assert period(datetime(2026, 1, 17, 19, 0)) == "cheias"  # Saturday
assert period(datetime(2026, 1, 18, 19, 0)) == "vazio"  # Sunday

# Season switches
assert season(date(2026, 3, 28)) == "winter"  # day before
assert season(date(2026, 3, 29)) == "summer"  # last Sunday of March
assert season(date(2026, 10, 24)) == "summer"
assert season(date(2026, 10, 25)) == "winter"  # last Sunday of October

# Weekly totals - the test that catches structural errors
assert weekly_hours("summer")["ponta"] == 15
assert weekly_hours("winter")["ponta"] == 25
assert weekly_hours("summer")["vazio"] == 76
assert weekly_hours("winter")["vazio"] == 76
```

### Validation against real data

The meter recorded, over 2 Jun – 1 Jul 2026: **56 kWh in ponta out of 644 kWh total (8.7%)**.

The model predicts, for a flat load in summer weekly cycle: 15/168 = **8.93%** → 57.5 kWh.

2.6% deviation. If a change to the calendar moves this number, the change is wrong.

> **Note (Checkpoint A):** 8.93% is the whole-week figure. Iterating
> the literal 30-day invoice window (Tue 2 Jun – Wed 1 Jul = 4 whole
> weeks plus a Tuesday and a Wednesday, both ponta-bearing days)
> gives 66/720 h = **9.17%**. The documented validation compares the
> whole-week model against the measured 8.7%; the measured value
> sitting below *both* models is a separate observation recorded in
> `docs/findings.md`.

The literal 30-day invoice window (starting Tue 2 Jun 2026) contains 22 ponta
days, giving 9.17%. The 8.93% figure above is the structural whole-week value
(15/168) and is what the test compares against. Measured 8.7% sits below both,
suggesting a small mid-morning dip or a slight ponta-window offset — revisit
with 15-minute load data in Phase 2.

---

## 5. OMIE reference series (MA30, €/MWh)

30-day moving averages by period, weekly cycle. Used in the savings estimates.

> **Window convention (Checkpoint A finding):** a row labeled month M
> covers the EDP **billing window [day 2 of M−1, day 2 of M)** — the
> invoice convention (e.g. 2 Jun – 1 Jul), not the calendar month.
> Under this alignment the Dec-25, May-26 and Jul-26 rows reproduce
> from raw OMIE data to ≤0.01%. A few cells were sampled on slightly
> different dates (notably Oct-25 ponta; see `docs/findings.md`);
> `tests/test_omie_validation.py` pins those individually.

> **Window convention:** a row labelled month M covers [day 2 of M−1, day 2 of M),
> matching the EDP billing cycle — not the calendar month. Dec-25, May-26 and
> Jul-26 reproduce to ≤0.01% under this alignment. Five values deviate and are
> pinned in `tests/test_omie_validation.py`; Oct-25 ponta (ref 24.09, computed
> 18.46) is unexplained and the reference is suspect — the window straddles the
> 26 Oct season switch.

| Month | Vazio | Cheias | Ponta |
|---|---|---|---|
| 2025-09 | 73.72 | 70.12 | 33.78 |
| 2025-10 | 67.11 | 66.92 | 24.09 |
| 2025-11 | 68.88 | 82.66 | 66.11 |
| 2025-12 | 55.58 | 63.97 | 67.83 |
| 2026-01 | 67.78 | 83.70 | 87.26 |
| 2026-02 | 58.46 | 74.66 | 82.52 |
| 2026-03 | 6.37 | 11.11 | 24.08 |
| 2026-04 | 37.41 | 39.28 | 58.62 |
| 2026-05 | 50.74 | 45.38 | 7.45 |
| 2026-06 | 63.85 | 55.23 | 5.11 |
| 2026-07 | 82.97 | 65.70 | 27.18 |
| 2026-08 | 117.23 | 111.70 | 74.18 |

### Daily cycle (for comparison)

| Month | Vazio | Cheias | Ponta | Simples |
|---|---|---|---|---|
| 2025-09 | 92.45 | 50.04 | 55.44 | 68.61 |
| 2025-10 | 82.73 | 46.78 | 54.68 | 63.08 |
| 2025-11 | 83.82 | 66.44 | 72.47 | 74.70 |
| 2025-12 | 62.26 | 52.95 | 75.75 | 60.63 |
| 2026-01 | 72.20 | 76.84 | 90.66 | 77.21 |
| 2026-02 | 61.33 | 67.92 | 84.80 | 67.99 |
| 2026-03 | 8.04 | 7.11 | 25.98 | 10.64 |
| 2026-04 | 46.26 | 27.92 | 62.10 | 41.25 |
| 2026-05 | 67.04 | 24.80 | 36.03 | 44.27 |
| 2026-06 | 85.49 | 28.66 | 43.77 | 54.86 |
| 2026-07 | 103.11 | 41.61 | 56.97 | 69.80 |
| 2026-08 | 149.42 | 79.53 | 93.24 | 110.94 |

**Consistency check:** `Simples = (10*Vazio + 10*Cheias + 4*Ponta) / 24` for the daily cycle.
Sep 2025: (10x92.45 + 10x50.04 + 4x55.44)/24 = **68.61** ✓

This identity is a good sanity test for any new data load.

---

## 6. Monthly reference arbitrage

Net value of moving 1 kWh from vazio to ponta, weekly cycle, K₁=1.08, η=0.90:

| Month | €/kWh | | Month | €/kWh |
|---|---|---|---|---|
| 2026-06 | 0.141 | | 2025-11 | 0.212 |
| 2026-07 | 0.142 | | 2025-12 | 0.233 |
| 2026-08 | 0.154 | | 2026-01 | 0.241 |
| 2025-10 | 0.161 | | 2026-04 | 0.247 |
| 2026-05 | 0.163 | | 2026-03 | 0.247 |
| 2025-09 | 0.164 | | **2026-02** | **0.248** |

**Weighted average: ~€0.196/kWh. Never negative in any month of the year.**

Decomposition:
- **TAR contribution: +€0.2276/kWh** — fixed, regulated, identical every month
- **Energy contribution: −€0.031/kWh** — a **cost**, in 9 of 12 months

This is the central fact of the domain: the arbitrage is on the TAR, and the market charges a ~14% toll to execute it.

---

## 7. Expected ERSE reform (~January 2027)

ERSE proposes moving BTN tri-horária ponta hours markedly to the end of the day, eliminating the morning window, **while preserving the daily duration of each period**.

Expected consequences:

- Ponta volume unchanged → overall economics preserved
- Ponta comes to coincide with the OMIE price peak → the energy component stops working against the TAR
- **Arbitrage should improve**, especially in summer

Action: keep `CALENDARS` versioned (ADR-0005) and fill in the 2027 table when published. No code change should be required.
