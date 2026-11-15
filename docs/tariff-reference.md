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

### Daily cycle (selectable since ADR-0012)

Identical every day of the week, weekends included. Not the owner's
choice: exposes 46% more consumption to ponta, costing ~€105/year
more. Lives in `core/calendar.py` (`DAILY_CALENDARS`) since ADR-0012:
the integration plans and prices with it when the configured tariff
uses the daily cycle, and the simulations compare it.

| Season | Vazio | Ponta | Cheias |
|---|---|---|---|
| Summer | 00:00–08:00, 22:00–24:00 | **10:30–13:00, 19:30–21:00** | 08:00–10:30, 13:00–19:30, 21:00–22:00 |
| Winter | 00:00–08:00, 22:00–24:00 | **09:00–10:30, 18:00–20:30** | 08:00–09:00, 10:30–18:00, 20:30–22:00 |

**Hours/week:** Vazio 70 · Ponta **28** · Cheias 70 (both seasons). Season
switches follow the weekly cycle (last Sunday of March / October).

### Bi-horária (both cycles)

Two periods: **vazio** and **fora de vazio**. In each cycle the bi-horária
vazio is exactly the tri-horária vazio above; fora de vazio is ponta plus
cheias. So bi-horária needs no calendar of its own — it prices ponta and
cheias alike (TAR: fora de vazio 0.0835, vazio 0.0158, §1).

### Source and verification status (daily cycle, bi-horária)

Added 2026-09-17 for the tariff comparison. Taken from
<https://www.tiagofelicia.pt/periodos-horarios.html>, a **secondary
source**, cross-checked three ways:

1. Its weekly tri-horária tables match §3 above exactly — the tables the
   invoice validated (8.7% ponta measured vs 8.93% modelled).
2. Its daily cycle matches this document's previous summary (ponta 4 h/day,
   vazio 22:00–08:00) and the §5 identity `(10·vazio + 10·cheias + 4·ponta)/24`.
3. Its bi-horária weekly vazio (including the Saturday spans) is identical
   to the tri-horária weekly vazio.

**Not yet read against ERSE directly:** ERSE's own period documents
(<https://www.erse.pt/media/s0wh5zf0/periodoshorariosele.pdf>) publish the
tables as graphics that could not be machine-read. Verify against them
before relying on a daily-cycle result for a contract decision — and,
since ADR-0012, before actuating a battery on a daily-cycle contract.
`tests/test_tariffs.py` pins every boundary, so a correction is a one-row
edit.

ERSE's period reform applies to BTN bi/tri-horária between July and
December 2027 (and drops the summer/winter distinction from the daily
cycle): a new row in `DAILY_CALENDARS`, never an edit (ADR-0005).

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
