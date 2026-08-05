"""
Reference OMIE series from docs/tariff-reference.md §5 (MA30, EUR/MWh).

Used to validate every fresh data load. Values are 30-day moving
averages by period as recorded in the project docs — do not edit them
to make a validation pass; a mismatch means the loaded data (or the
calendar, or the timezone mapping) is wrong, or the deviation must be
reported and understood.
"""

from __future__ import annotations

MonthKey = tuple[int, int]

# Weekly cycle (the one this project uses): month -> vazio, cheias, ponta.
WEEKLY_MA30: dict[MonthKey, dict[str, float]] = {
    (2025, 9): {"vazio": 73.72, "cheias": 70.12, "ponta": 33.78},
    (2025, 10): {"vazio": 67.11, "cheias": 66.92, "ponta": 24.09},
    (2025, 11): {"vazio": 68.88, "cheias": 82.66, "ponta": 66.11},
    (2025, 12): {"vazio": 55.58, "cheias": 63.97, "ponta": 67.83},
    (2026, 1): {"vazio": 67.78, "cheias": 83.70, "ponta": 87.26},
    (2026, 2): {"vazio": 58.46, "cheias": 74.66, "ponta": 82.52},
    (2026, 3): {"vazio": 6.37, "cheias": 11.11, "ponta": 24.08},
    (2026, 4): {"vazio": 37.41, "cheias": 39.28, "ponta": 58.62},
    (2026, 5): {"vazio": 50.74, "cheias": 45.38, "ponta": 7.45},
    (2026, 6): {"vazio": 63.85, "cheias": 55.23, "ponta": 5.11},
    (2026, 7): {"vazio": 82.97, "cheias": 65.70, "ponta": 27.18},
    (2026, 8): {"vazio": 117.23, "cheias": 111.70, "ponta": 74.18},
}

# Daily cycle (reference only): month -> vazio, cheias, ponta, simples.
# The daily cycle has 10 h vazio, 10 h cheias and 4 h ponta every day,
# hence the consistency identity Simples = (10*V + 10*C + 4*P) / 24.
DAILY_MA30: dict[MonthKey, dict[str, float]] = {
    (2025, 9): {"vazio": 92.45, "cheias": 50.04, "ponta": 55.44, "simples": 68.61},
    (2025, 10): {"vazio": 82.73, "cheias": 46.78, "ponta": 54.68, "simples": 63.08},
    (2025, 11): {"vazio": 83.82, "cheias": 66.44, "ponta": 72.47, "simples": 74.70},
    (2025, 12): {"vazio": 62.26, "cheias": 52.95, "ponta": 75.75, "simples": 60.63},
    (2026, 1): {"vazio": 72.20, "cheias": 76.84, "ponta": 90.66, "simples": 77.21},
    (2026, 2): {"vazio": 61.33, "cheias": 67.92, "ponta": 84.80, "simples": 67.99},
    (2026, 3): {"vazio": 8.04, "cheias": 7.11, "ponta": 25.98, "simples": 10.64},
    (2026, 4): {"vazio": 46.26, "cheias": 27.92, "ponta": 62.10, "simples": 41.25},
    (2026, 5): {"vazio": 67.04, "cheias": 24.80, "ponta": 36.03, "simples": 44.27},
    (2026, 6): {"vazio": 85.49, "cheias": 28.66, "ponta": 43.77, "simples": 54.86},
    (2026, 7): {"vazio": 103.11, "cheias": 41.61, "ponta": 56.97, "simples": 69.80},
    (2026, 8): {"vazio": 149.42, "cheias": 79.53, "ponta": 93.24, "simples": 110.94},
}


def daily_simples_identity(month: MonthKey) -> float:
    """Simples implied by the daily-cycle identity (10*V + 10*C + 4*P)/24."""
    row = DAILY_MA30[month]
    return (10 * row["vazio"] + 10 * row["cheias"] + 4 * row["ponta"]) / 24
