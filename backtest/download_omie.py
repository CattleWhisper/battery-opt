"""
Download historical OMIE day-ahead marginal price files (marginalpdbc).

Fetches one file per delivery day from omie.es into backtest/data/omie/
(gitignored — ADR-0001 keeps bulk data out of git; only small fixtures
under tests/fixtures/ are committed).

The window is 2025-08-31 .. 2026-09-01: one day of margin on each side
of the Sep 2025 - Aug 2026 analysis window, because OMIE delivery days
are defined in Europe/Madrid time and each file's first period lands at
23:00 of the previous day in Europe/Lisbon.

Idempotent: existing complete files (trailing '*' sentinel) are kept.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

FIRST_DAY = date(2025, 8, 31)
LAST_DAY = date(2026, 9, 1)
DATA_DIR = Path(__file__).parent / "data" / "omie"
URL = "https://www.omie.es/es/file-download?parents=marginalpdbc&filename={filename}"
POLITE_DELAY_S = 0.15
RETRIES = 3


MAX_SUFFIX = 9  # republished days get .2, .3, ... (e.g. 20251030 is .3)


def _have_complete(stamp: str) -> bool:
    """Report whether any complete version of the day's file is present."""
    for path in DATA_DIR.glob(f"marginalpdbc_{stamp}.*"):
        try:
            if path.read_text().rstrip().endswith("*"):
                return True
        except OSError:
            continue
    return False


def _fetch(filename: str) -> str | None:
    """Fetch one file; None if this version does not exist (404)."""
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        URL.format(filename=filename),
        headers={"User-Agent": "battery-opt backtest (personal use)"},
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if attempt == RETRIES - 1:
                raise
        except OSError:
            if attempt == RETRIES - 1:
                raise
        time.sleep(2.0 * (attempt + 1))
    return None


def _fetch_day(stamp: str) -> None:
    """Fetch the first published version of a delivery day's file."""
    for suffix in range(1, MAX_SUFFIX + 1):
        filename = f"marginalpdbc_{stamp}.{suffix}"
        body = _fetch(filename)
        if body is None:
            continue
        if not body.rstrip().endswith("*"):
            msg = f"Incomplete or unexpected payload for {filename}"
            raise ValueError(msg)
        (DATA_DIR / filename).write_text(body)
        return
    msg = f"No published version found for {stamp} (tried .1-.{MAX_SUFFIX})"
    raise FileNotFoundError(msg)


def download(first_day: date = FIRST_DAY, last_day: date = LAST_DAY) -> int:
    """Download all files in the window; return the number fetched."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetched = 0
    day = first_day
    while day <= last_day:
        stamp = day.strftime("%Y%m%d")
        if not _have_complete(stamp):
            _fetch_day(stamp)
            fetched += 1
            time.sleep(POLITE_DELAY_S)
            if fetched % 50 == 0:
                print(f"...{fetched} files fetched, at {day}")
        day = day + timedelta(days=1)
    return fetched


if __name__ == "__main__":
    try:
        download()
    except FileNotFoundError as err:
        # Day-ahead files only exist through tomorrow (published ~13:30
        # CET). Re-run after new auctions to extend the series.
        print(f"Stopped at the publication frontier: {err}")
    total = len(list(DATA_DIR.glob("marginalpdbc_*.*")))
    print(f"{total} files present in {DATA_DIR}")
    sys.exit(0)
