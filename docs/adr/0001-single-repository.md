# ADR-0001: Single repository with an HA-free `core/`

**Status:** Accepted · **Date:** 2026-08

## Context

The project has three execution contexts: unit tests (pytest), a backtest over 12 months of historical data, and production inside Home Assistant. The domain logic — tariff calendar, price model, optimiser — is identical in all three.

The alternative considered was two repositories: a `battery-opt` package published to PyPI, and a `ha-battery-opt` integration declaring it in `manifest.json`.

## Decision

**One repository.** Domain logic lives in `custom_components/battery_opt/core/` and **imports nothing from `homeassistant`**.

```
battery-opt/
  custom_components/battery_opt/
    core/                    <- zero HA imports
    coordinator.py sensor.py driver.py __init__.py manifest.json
  tests/
  backtest/
```

## Rationale

- HACS downloads only `custom_components/battery_opt/`. Tests, backtest and OMIE data stay in the repo and never reach the HA instance — the bloat concern resolves itself.
- Two repositories would force publishing the core to PyPI (`manifest.json` needs an installable `requirements`), with version bumps on both sides for every change. Disproportionate ceremony for a single consumer.
- Git URLs in `requirements` are fragile and HACS validation dislikes them.
- With `core/` free of HA, the same code runs under pytest, in the backtest and in production — no divergence.

## Consequences

- `conftest.py` at the repo root so `custom_components` resolves as a package.
- Large OMIE series stay out of git (or in LFS); commit only a small fixture for tests.
- Tag releases from the start — HACS uses tags.
- **If the calendar and price model ever prove useful to others** (nobody has packaged the ERSE tri-horária periods or the Portuguese indexed formula), extraction is clean precisely because `core/` has no HA dependency. Do not build for that possibility now.
