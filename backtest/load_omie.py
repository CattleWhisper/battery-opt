"""
Load historical OMIE marginalpdbc files into a tabular series.

Granularity of the raw data (resolves Open Question #1 for the
historical series):

- through 2025-09-30 the market was hourly: 24 periods per day;
- from 2025-10-01 (SDAC 15-minute MTU go-live) it is quarter-hourly:
  96 periods per day, 92 on the spring DST switch and 100 on the
  autumn one.

File format, verbatim from omie.es: a 'MARGINALPDBC;' header line,
rows 'YYYY;MM;DD;period;price_pt;price_es;' and a trailing '*'
sentinel. Periods are 1-based from 00:00 Europe/Madrid (market time);
prices are EUR/MWh. The first price column is taken as the Portuguese
system price — cross-checked empirically by the MA30 validation, which
would drift on any month with PT/ES market splitting if the columns
were swapped.

Interval starts are converted to Europe/Lisbon (elapsed-time
arithmetic in UTC, so DST switch days come out right).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.battery_opt.core.calendar import Period, period

TZ_MADRID = ZoneInfo("Europe/Madrid")
TZ_LISBON = ZoneInfo("Europe/Lisbon")

DATA_DIR = Path(__file__).parent / "data" / "omie"

_HOURLY_MAX_PERIODS = 25  # 23/24/25 periods -> hourly; 92/96/100 -> quarter-hourly


@dataclass(frozen=True)
class PriceRecord:
    """One market interval: aware Lisbon start, duration, PT price."""

    start: datetime
    duration_hours: float
    price_eur_mwh: float


def parse_file(path: Path) -> list[PriceRecord]:
    """Parse one marginalpdbc file into Lisbon-time price records."""
    rows: list[tuple[date, int, float]] = []
    for line in path.read_text().splitlines():
        fields = line.strip().split(";")
        if len(fields) < 6 or not fields[0].isdigit():
            continue  # header, sentinel or blank
        delivery = date(int(fields[0]), int(fields[1]), int(fields[2]))
        rows.append((delivery, int(fields[3]), float(fields[4])))

    duration = 1.0 if len(rows) <= _HOURLY_MAX_PERIODS else 0.25
    records = []
    for delivery, period_index, price in rows:
        midnight_utc = datetime(
            delivery.year, delivery.month, delivery.day, tzinfo=TZ_MADRID
        ).astimezone(UTC)
        start = midnight_utc + timedelta(hours=(period_index - 1) * duration)
        records.append(
            PriceRecord(
                start=start.astimezone(TZ_LISBON),
                duration_hours=duration,
                price_eur_mwh=price,
            )
        )
    return records


def load_series(data_dir: Path, first_day: date, last_day: date) -> list[PriceRecord]:
    """
    Load all delivery days in [first_day, last_day] into one sorted series.

    Each day has exactly one published file version on disk; if several
    are present, the highest suffix (latest republication) wins.
    """
    records: list[PriceRecord] = []
    day = first_day
    while day <= last_day:
        stamp = day.strftime("%Y%m%d")
        versions = sorted(
            data_dir.glob(f"marginalpdbc_{stamp}.*"),
            key=lambda p: int(p.suffix.lstrip(".")),
        )
        if not versions:
            msg = f"Missing OMIE file for {day} in {data_dir}"
            raise FileNotFoundError(msg)
        records.extend(parse_file(versions[-1]))
        day = day + timedelta(days=1)
    return sorted(records, key=lambda rec: rec.start)


MonthKey = tuple[int, int]


def monthly_period_averages(
    records: list[PriceRecord],
) -> dict[MonthKey, dict[Period, float]]:
    """
    Duration-weighted mean price per (Lisbon month, tariff period).

    Buckets through core.calendar.period(), weekly cycle — so this
    doubles as an end-to-end check of the calendar and the timezone
    mapping when compared against the §5 MA30 reference series.
    """
    sums: dict[tuple[MonthKey, Period], float] = defaultdict(float)
    hours: dict[tuple[MonthKey, Period], float] = defaultdict(float)
    for rec in records:
        key = ((rec.start.year, rec.start.month), period(rec.start))
        sums[key] += rec.price_eur_mwh * rec.duration_hours
        hours[key] += rec.duration_hours
    result: dict[MonthKey, dict[Period, float]] = defaultdict(dict)
    for (month, period_name), total in sums.items():
        result[month][period_name] = total / hours[(month, period_name)]
    return dict(result)


def monthly_simples(records: list[PriceRecord]) -> dict[MonthKey, float]:
    """Duration-weighted mean over all hours per Lisbon month (no calendar)."""
    sums: dict[MonthKey, float] = defaultdict(float)
    hours: dict[MonthKey, float] = defaultdict(float)
    for rec in records:
        key = (rec.start.year, rec.start.month)
        sums[key] += rec.price_eur_mwh * rec.duration_hours
        hours[key] += rec.duration_hours
    return {month: sums[month] / hours[month] for month in sums}


def _in_window(rec: PriceRecord, first_day: date, end_day: date) -> bool:
    """Report whether the record starts inside [first_day, end_day) Lisbon."""
    lo = datetime(first_day.year, first_day.month, first_day.day, tzinfo=TZ_LISBON)
    hi = datetime(end_day.year, end_day.month, end_day.day, tzinfo=TZ_LISBON)
    return lo.timestamp() <= rec.start.timestamp() < hi.timestamp()


def window_period_averages(
    records: list[PriceRecord],
    first_day: date,
    end_day: date,
) -> dict[Period, float]:
    """
    Duration-weighted mean price per period over [first_day, end_day).

    Used to reproduce the §5 reference series, whose rows follow the
    EDP billing window (day 2 of the previous month through day 1 of
    the labeled month), not calendar months.
    """
    sums: dict[Period, float] = defaultdict(float)
    hours: dict[Period, float] = defaultdict(float)
    for rec in records:
        if _in_window(rec, first_day, end_day):
            name = period(rec.start)
            sums[name] += rec.price_eur_mwh * rec.duration_hours
            hours[name] += rec.duration_hours
    return {name: sums[name] / hours[name] for name in sums}


def window_simples(
    records: list[PriceRecord],
    first_day: date,
    end_day: date,
) -> float:
    """Duration-weighted mean over all hours in [first_day, end_day)."""
    total = 0.0
    hours = 0.0
    for rec in records:
        if _in_window(rec, first_day, end_day):
            total += rec.price_eur_mwh * rec.duration_hours
            hours += rec.duration_hours
    return total / hours
