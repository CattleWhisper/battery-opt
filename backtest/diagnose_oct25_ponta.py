"""
One-off diagnostic: the Oct-2025 ponta reference deviation (Fix 2).

The §5 weekly reference row (2025, 10) reads ponta 24.09 EUR/MWh; the
pipeline computes 18.46 over the billing window [Sep 2, Oct 2) while
the same row's vazio matches that window exactly (67.11, 0.00%).

Checkpoint A review hypothesis: a season-handling artifact around the
summer->winter switch (26 Oct). Two candidate windows are in play —
[Sep 2, Oct 2) (the empirically anchored one) and [Oct 2, Nov 2) (the
review's reading, which straddles the switch) — so this computes the
ponta average under three season treatments over BOTH windows:

  a) whole window treated as summer   (ponta 09:15-12:15 Mon-Fri)
  b) whole window treated as winter   (ponta 09:30-12:00, 18:30-21:00)
  c) switching on 26 Oct              (current calendar behaviour)

Not a test: run manually with
  PYTHONPATH=. .venv/bin/python backtest/diagnose_oct25_ponta.py
Conclusion recorded in docs/findings.md; the pinned value in
tests/test_omie_validation.py stays.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from backtest.load_omie import DATA_DIR, PriceRecord, load_series
from custom_components.battery_opt.core.calendar import CALENDARS, season

TZ_LISBON = ZoneInfo("Europe/Lisbon")
TABLE = CALENDARS[-1][1]
REFERENCE = 24.09

_SATURDAY = 5
_SUNDAY = 6


def _day_class(d: date) -> str:
    if d.weekday() == _SUNDAY:
        return "sunday"
    if d.weekday() == _SATURDAY:
        return "saturday"
    return "weekday"


def ponta_average(
    records: list[PriceRecord],
    first_day: date,
    end_day: date,
    treatment: str,
) -> float:
    """Ponta mean over [first_day, end_day) under a season treatment."""
    lo = datetime(first_day.year, first_day.month, first_day.day, tzinfo=TZ_LISBON)
    hi = datetime(end_day.year, end_day.month, end_day.day, tzinfo=TZ_LISBON)
    total = hours = 0.0
    for rec in records:
        if not lo.timestamp() <= rec.start.timestamp() < hi.timestamp():
            continue
        season_name = season(rec.start.date()) if treatment == "calendar" else treatment
        spans = TABLE[season_name][_day_class(rec.start.date())]
        minute = rec.start.hour * 60 + rec.start.minute
        if any(s <= minute < e for s, e, name in spans if name == "ponta"):
            total += rec.price_eur_mwh * rec.duration_hours
            hours += rec.duration_hours
    return total / hours


def main() -> None:
    """Print the diagnosis matrix."""
    records = load_series(DATA_DIR, date(2025, 9, 1), date(2025, 11, 2))
    windows = {
        "[Sep 2, Oct 2)  - anchored by exact vazio match": (
            date(2025, 9, 2),
            date(2025, 10, 2),
        ),
        "[Oct 2, Nov 2)  - straddles the 26 Oct switch": (
            date(2025, 10, 2),
            date(2025, 11, 2),
        ),
    }
    print(f"Reference (2025, 10) ponta: {REFERENCE}\n")
    for label, (first_day, end_day) in windows.items():
        print(label)
        for treatment in ("summer", "winter", "calendar"):
            value = ponta_average(records, first_day, end_day, treatment)
            marker = "  <-- matches" if abs(value - REFERENCE) < 1.5 else ""
            print(f"  {treatment:9} {value:7.2f}{marker}")
        print()


if __name__ == "__main__":
    main()
