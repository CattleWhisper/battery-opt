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
  - **(2025-10) ponta**: ref 24.09 vs computed 18.46 (−23%). Same
    window's vazio matches exactly, so the data is identical — the
    reference used a different ponta hour-set or window for that row.
    Unresolved; low stakes (few, cheap ponta hours; the TAR dominates).
  - **(2026-03) vazio and simples**: the reference's March window
    excluded Mar 1 (vazio 6.43 vs ref 6.37 without it). February is
    the year's cheapest month, so one March day moves the mean 5–10%.
  - **(2026-04) cheias, (2026-08) cheias/ponta**: window-edge
    differences of 2.2–4.5%.
- The §5 daily table is internally consistent: its Simples column
  equals (10·V + 10·C + 4·P)/24 to 0.1% in all 12 months, and our
  all-hours means match that identity within 2% (March pinned as
  above).

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
