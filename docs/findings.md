# Findings

Empirical results produced by Phase 0. Each entry states what was
measured, on what data, and what it resolves.

---

## Task 3 — OMIE historical ingestion (Checkpoint A, 2026-08-05)

### Granularity (resolves Open Question #1 for the historical series)

The raw `marginalpdbc` series changes resolution mid-window:

| Range | Resolution | Periods/day |
|---|---|---|
| through 2025-09-30 | **hourly** | 24 |
| from 2025-10-01 (SDAC 15-minute MTU go-live) | **quarter-hourly** | 96 (92 spring DST, 100 autumn DST) |

Consequences: the backtest has true quarter-hourly prices for 11 of the
12 months; September 2025 is hourly and any quarter-hourly plan for it
repeats each hourly price ×4. What the HACS `omie` integration exposes
in production (the other half of Open Question #1) still needs to be
confirmed in Phase 2 — it wraps the same market data, so quarter-hourly
should be available going forward.

The delivery day is defined in Europe/Madrid (market time): period 1 =
00:00 CET/CEST = **23:00 of the previous day in Europe/Lisbon**. All
timestamps are converted; DST switch days carry 23 h / 25 h and land on
Sundays (all vazio) in both timezones.

Files are republished with incrementing suffixes (`.1`, `.2`, `.3` —
e.g. 2025-10-30 exists only as `.3`); the downloader takes the first
available version and the loader the highest one on disk. Day-ahead
files exist only through tomorrow's delivery day (published ~13:30 CET).

### Validation against the §5 reference series

The reference rows labeled month M cover the **EDP billing window
[day 2 of M−1, day 2 of M)** — the invoice convention (2 Jun – 1 Jul),
not calendar months. Discovered empirically; under it:

- **Dec-25, May-26 and Jul-26 reproduce to ≤0.01% in all three
  periods**, and Oct-25 vazio, Feb-26 vazio and Mar-26 ponta are exact.
  This jointly proves the parsing, the Madrid→Lisbon mapping, the PT
  price column, and the tri-horária weekly calendar over real data.
- 28 of 33 weekly-table values fall within the 2% tolerance.
- Five values deviate and are pinned in `tests/test_omie_validation.py`
  as known deviations rather than hidden by a looser tolerance:
  - **(2025-10) ponta**: ref 24.09 vs computed 18.46 (−23%). Diagnosed
    — see "Oct-2025 ponta diagnostic" below. The reference cell is the
    artifact; the implementation is correct.
  - **(2026-03) vazio and simples**: the reference's March window
    excluded Mar 1 (vazio 6.43 vs ref 6.37 without it). February is
    the year's cheapest month, so one March day moves the mean 5–10%.
  - **(2026-04) cheias, (2026-08) cheias/ponta**: window-edge
    differences of 2.2–4.5%.
- The §5 daily table is internally consistent: its Simples column
  equals (10·V + 10·C + 4·P)/24 to 0.1% in all 12 months, and our
  all-hours means match that identity within 2% (March pinned as
  above).

### Oct-2025 ponta diagnostic (Fix 2, Checkpoint A review)

`backtest/diagnose_oct25_ponta.py` computed the row's ponta under
three season treatments over both candidate windows:

| Window | summer | winter | calendar (switch 26 Oct) |
|---|---|---|---|
| [Sep 2, Oct 2) — anchored by the exact vazio match | **18.46** | 76.63 | 18.46 |
| [Oct 2, Nov 2) — straddles the season switch | 51.80 | 91.65 | 64.81 |

**No treatment over either window reproduces 24.09.** The review's
season-straddle hypothesis is refuted in direction: winter hours add
the 18:30–21:00 evening peak, which is *expensive* on OMIE, pushing
the average up to 76–92, not down to 24.

What does fit, verified by sliding the window: under correct summer
hours a trailing 30-day ponta average crosses 24.09 between end-dates
of 6 Oct (23.70) and 7 Oct (24.98) — early-October midday prices had
jumped (44.29 over 2–6 Oct vs 18.46 in September). So the row's ponta
cell was sampled ~5 days later than its vazio cell (which matches
[Sep 2, Oct 2) to 0.00%): the reference row mixes sampling dates.

Conclusion: **the reference cell is the artifact, the implementation
is correct**; the calendar is unchanged and the pinned value stays.
Side effect: §6's Oct-25 arbitrage (0.161 €/kWh) is slightly
understated — with the consistent-window ponta (18.46) it is ~0.155;
the TAR dominates either way.

### Measured ponta share sits below both models

The invoice measured 8.7% of consumption in ponta; the whole-week
flat-load model gives 8.93% and the literal invoice window (4 whole
weeks + Tue + Wed) gives 9.17%. The measurement sitting **below both**
suggests a small mid-morning consumption dip or a slight offset of the
real ponta window — either way, slightly less ponta volume than the
flat model assumes, making savings estimates **mildly optimistic**.
Revisit in Phase 2 with 15-minute load data from the meter.

### PT/ES market splitting

3,198 of 30,504 intervals (10.5%, across 177 days) have different
Portuguese and Spanish prices. Splitting is common enough that the
exact reference matches above also confirm the column-order assumption
(first price column = Portugal) empirically.

### Data inventory

- `backtest/data/omie/` (gitignored): 341 files, delivery days
  2025-08-31 … 2026-08-06 (the publication frontier on 2026-08-05).
  Re-run `backtest/download_omie.py` to extend.
- `tests/fixtures/omie/` (committed): four real files — hourly
  (2025-09-15), quarter-hourly (2026-04-15), autumn DST (2025-10-26,
  100 periods), spring DST (2026-03-29, 92 periods).

---

## Checkpoint B decisions (2026-08-05, owner review)

1. **Phase 1: GO.** The €30/yr gate was cleared 4.4× and the owner
   confirmed proceeding to Tasks 7–9. Open question #3 (is
   zero-export enforced by the device or must the plan limit power?)
   still blocks Task 7 and needs a check on the physical device.
2. **Cheias cycling: capped at plan_wear = 0.0467 (WEAR_COST_MAX).**
   The cap is a planning wear margin: the optimiser plans with the
   full replacement-cost wear bound already documented in CONTEXT.md,
   pruning the least profitable cycles first; savings are always
   booked at the true €0.020/kWh. Chosen point: **375 cycles/yr
   (−10%), €323.64/yr — keeps 97.6% of the uncapped gain**, cycle
   life ~16 years. The measured gain-vs-cycles frontier
   (`backtest/run.py --plan-wear`, savings incl. VAT vs static
   196.10):

   | plan_wear | cycles/yr | saving €/yr | % of max gain |
   |---|---|---|---|
   | 0.020 (uncapped) | 417 | 326.73 | 100.0% |
   | 0.040 | 386 | 325.08 | 98.7% |
   | **0.0467 (chosen)** | **375** | **323.64** | **97.6%** |
   | 0.060 | 363 | 321.22 | 95.8% |
   | 0.080 | 343 | 315.28 | 91.2% |
   | 0.100 | 314 | 303.18 | 82.0% |
   | 0.130 | 240 | 262.33 | 50.7% |
   | 0.160 | 174 | 214.23 | 13.9% |
   | 0.200 | 101 | 146.46 | −38.0% (loses to static) |

   The frontier is flat at the top — marginal cheias cycles earn
   almost nothing individually — and falls off a cliff past ~0.10.
3. **Reference figures: updated to measured.** CONTEXT.md
   §Reference figures and the docs/plan.md Task 5/6 lines now carry
   the backtested numbers (static €196.10, dynamic €326.73, second
   unit +€110.64); the superseded MA30-derived estimates (~€267,
   €300–345, ~€45) are noted as such.
4. **Second unit: not purchased.** Threshold met marginally
   (+€110.64/yr) but ~12.7-yr payback; recommendation accepted.
   Revisit only if the ERSE 2027 reform reshapes the economics.
5. **Zero-export (open question #3): the device enforces it.** The
   Marstek clamps discharge to consumption via its smart meter, so
   C-1 in the plan is a defensive check, not the only line of
   defence. Task 7 (driver) is unblocked; the executor still
   validates every plan against C-1..C-7 before actuating (defence
   in depth, spec §11).

---

## Task 6 — Backtest results and Checkpoint B numbers (2026-08-05)

Method: `backtest/run.py` replays each strategy over the 11
quarter-hourly months (Oct 1 2025 – Aug 6 2026, 310 days), chaining
each day's ending SoC into the next start. Cost accounting is the
core evaluator; every daily plan is re-validated against C-1..C-7.
Annual figures are season-weighted (154 winter / 211 summer days),
include VAT where stated, and are net of the €0.020/kWh wear cost.
Flat 1,040 W load, no solar, both strategies at default parameters
unless noted. Sep-2025 (hourly-only data) is reported separately.

| Configuration | Saving €/yr incl. VAT | Cycles/yr |
|---|---|---|
| Static, 5 kWh | **196.10** | 179 |
| Greedy, 5 kWh | **326.73** | 417 |
| Greedy, 10 kWh | 437.37 | 350 (of 10 kWh) |
| Greedy, Média billing (K₁ 1.10) | 229.05 | 254 |
| Greedy, hourly-degraded prices | 319.73 | 421 |
| Greedy, 13:00 planning boundary | 331.68 | 354 |

### The four Checkpoint B answers

1. **Dynamic vs static: +€130.64/yr** (326.73 vs 196.10) — 4.4× the
   €30 go/no-go threshold. Robust to wear: the gain is €161.28 at
   wear 0, €130.64 at 0.020, €96.90 at the €0.0467 maximum.
2. **Second unit: +€110.64/yr** (437.37 vs 326.73) — above the €100
   reconsideration threshold, well above the docs' ~€45 estimate.
   Drivers: winter weekdays demand 5.2 kWh of ponta but one unit
   delivers only 3.46 past the floor, and the larger unit also runs
   more cheias arbitrage. At ~€1,400 the payback is ~12.7 years.
3. **Horária vs Média: Horária wins by €135.40/yr** billed
   (1,482.98 vs 1,618.38). Under Média the dynamic-vs-static gain
   collapses to €55.65 — intra-period selection is most of the
   dynamic edge, and Média bills it away. Confirms the tariff choice.
4. **Switch when the 35% promo expires: yes by these numbers —
   €263.36/yr** (billed 1,482.98 on Horária+greedy vs 1,746.34 on
   Simples-15) vs the ~€183 estimate in CONTEXT.md. Above the €100
   threshold. On Simples-15 the battery must idle: running the
   static schedule anyway would *lose* €38.43/yr (wear + round-trip
   losses at a flat price). Caveat: the Simples-15 run assumes the
   same daily fixed terms as the indexed tariff, so the comparison
   is the energy component only.

### Flags for the human review

- **Cheias cycling (spec §11 "ask first").** The greedy runs 417
  cycles/yr vs the static's 179 — much of the dynamic gain comes
  from extra low-margin cycles into cheias, each clearing the C-8
  bar net of wear. Cycle life at 6,000 rated: ~14 years (greedy) vs
  ~34 (static); the 10-year warranty still binds first, and wear is
  priced into every figure above. This behaviour needs an explicit
  blessing before Phase 2 enables it in production.
- **The ~€267 static reference is superseded: measured €196.10.**
  Causes, in order: the docs assumed 100% ponta coverage but winter
  weekdays demand 5.2 kWh against 3.46 deliverable (67%); wear was
  not netted (~€21/yr at 0.020); the MA30 seasonal misalignment.
  `CONTEXT.md` §Reference figures and `docs/plan.md` Task 5/6 lines
  (~€267, €300–345 dynamic, ~€45 second unit) need updating —
  **pending owner confirmation, not edited**.
- Dynamic €326.73 lands inside the docs' €300–345 target band.

### Side measurements

- **Quarter-hourly resolution is worth ~€7.00/yr** (326.73 native vs
  319.73 with prices degraded to hourly means). Sep-2025 being
  hourly-only is therefore immaterial; hourly fallbacks in
  production would cost ~2% of the saving.
- **Sep-2025 (hourly data, separate):** greedy €0.70/day vs static
  €0.39/day excl. VAT — consistent with the main window's ratio.
- **Day boundary (open question #4): not material.** Re-planning on
  a 13:00–13:00 window (letting the greedy pair the midday trough
  with next morning's ponta) adds €4.95/yr (+1.5%) and cuts cycles
  from 417 to 354. The midnight boundary with SoC chaining is fine
  for v1; a 48 h horizon is an efficiency refinement, not a
  materially different answer.
- Wear sensitivity (greedy, 5 kWh): €378.22 / €326.73 / €265.17 at
  wear 0 / 0.020 / 0.0467 — cycling volume responds (471/417/375),
  the decision does not.

Per-day CSVs for these runs are under `backtest/data/results/`
(gitignored; regenerate with the commands in `backtest/run.py`).

---

## Negative OMIE prices

Delivered price = OMIE/1000 × 1.164 × 1.08 + 0.0185 + TAR. K2 and TAR are added
regardless of sign, so a negative OMIE does not produce a negative bill:

| Period | Delivered price at OMIE = −20 €/MWh |
|---|---|
| Vazio | +€0.0092/kWh |
| Cheias | +€0.0347/kWh |
| Ponta | +€0.2386/kWh |

Break-even OMIE for a zero delivered price: ~−27 €/MWh (vazio), −48 (cheias),
−209 (ponta). The first two are reachable on extreme days; the third is not.

Strategy is unaffected. The pairing condition `price[d] > price[c]/η + WEAR_COST`
handles negative prices correctly without a special case: a negative `price[c]`
divided by 0.90 becomes more negative, which is right — if you are paid to
consume, conversion losses become revenue rather than cost.

A negative price can never invert the ponta/vazio ranking; that would require
`OMIE_ponta − OMIE_vazio < −182 €/MWh`, which does not occur.

**Implications for Task 4:** nothing may assume prices are positive — no `abs()`,
no `max(0, price)`. The property-test generator must draw from roughly −50 to
+300 €/MWh, not 0 to 300. The parser should reject values outside the SDAC
harmonised clearing limits (~−500 to +4000 €/MWh) as parse errors rather than
trusting them.
