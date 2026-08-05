# ADR-0005: Tariff calendar versioned by effective date

**Status:** Accepted · **Date:** 2026-08

## Context

BTN tri-horária time periods are set by ERSE and they change. Around January 2027, ponta hours are expected to move to the end of the day, eliminating the morning window while preserving each period's daily duration.

A calendar hardcoded to 2026 would, from that date, produce plans that charge and discharge in the wrong hours — **silently**, with no error and no alarm.

## Decision

The calendar is a data structure indexed by effective date, not code:

```python
CALENDARS = [
    (date(2026, 1, 1), CALENDAR_2026),
    (date(2027, 1, 1), CALENDAR_2027),   # to be filled when ERSE publishes
]

def period(dt: datetime) -> Period:
    table = _effective_table(dt.date())
    ...
```

## Rationale

- Adding 2027 becomes a data change, not a logic change. Existing tests continue to cover 2026.
- The backtest over historical data automatically uses the correct table for each date — essential to avoid contaminating the analysis.
- It makes explicit that the calendar is an external regulatory input, not a domain constant.
- The same structure accommodates future TAR revisions, which ERSE issues annually.

## Consequences

- Requires a test table pinning weekly totals per effective version (15 h ponta in summer, 25 h in winter, for 2026). See `docs/tariff-reference.md`.
- When the 2027 table is published, expected totals must be recomputed and the test deliberately updated — which is the intended safeguard.
- Economics should **improve** with the reform: ponta comes to coincide with the OMIE peak, so the energy component stops working against the TAR.
