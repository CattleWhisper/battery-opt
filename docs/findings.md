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
