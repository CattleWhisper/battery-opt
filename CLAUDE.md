# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS) that plans charge/discharge of a Marstek Venus E 3.0 home battery to arbitrage the Portuguese regulated network tariff (TAR), not the OMIE market price. **Read `CONTEXT.md` before changing any code** — it holds the domain glossary, all constants, the invariants, and the known traps. `docs/spec.md` (design) and `docs/plan.md` (phased task breakdown) are the working documents; `docs/adr/` records the architecture decisions (0001–0008); `docs/tariff-reference.md` holds the verifiable tariff tables and the mandatory calendar test cases; `docs/findings.md` holds the measured results and the decision registers.

**Current state (2026-08-13):** Phase 0 is complete and Checkpoint B passed (dynamic +€131/yr measured over static — GO). The real integration lives at `custom_components/battery_opt/` (the blueprint template is gone): `core/` (HA-free, backtested over 11 months of real OMIE data) plus the full Phase 1/2 shell — config flow, coordinator, sensors, manual-override switches, driver, the ADR-0006 three-state executor and the ADR-0007 charge-power loop, through Task 15, and the Task 12 dynamic-actuation swap shipped behind the `dry_run` config option (default ON — the executor stays on the static plan until Checkpoint C). Prices come from HA core's OMIE integration (`get_prices_for_date` service). The spec §8 on-device checklist is complete (2026-08-11; headline: no firmware watchdog — the 42011 backstop is load-bearing) and the integration actuates the battery in supervised production. Remaining before Checkpoint C: the Task 9 power-off drill, then the 2-week static soak (`docs/validation-checklist.md`).

## Commands

```bash
scripts/setup      # install dependencies (pip, requirements_dev.txt)
scripts/lint       # ruff format . && ruff check . --fix
scripts/develop    # run a local Home Assistant instance with the integration loaded
pytest             # run tests; single file: pytest tests/test_calendar.py
```

- Ruff targets Python 3.14 with `select = ["ALL"]` (`.ruff.toml`) — expect strict linting, including full docstring and annotation rules.
- `scripts/develop` exports `PYTHONPATH=custom_components` and starts `hass --config ./config --debug`; HA config lives in `config/configuration.yaml`.
- Home Assistant version is pinned in `requirements_dev.txt` and `hacs.json` (currently 2026.6.4).

## Architecture

Target layout (ADR-0001 — single repo, HA-free core):

```
custom_components/battery_opt/
  core/          # calendar, prices, optimiser, forecast — ZERO homeassistant imports
  coordinator.py sensor.py driver.py executor.py config_flow.py ...
tests/           # pytest; conftest.py at repo root resolves custom_components
backtest/        # 12-month OMIE replay; only a small data fixture is committed
```

Dependency order is bottom-up and strict: `calendar.py` → `prices.py` → `optimiser.py` → backtest → coordinator/sensors → executor/driver. The calendar is built and validated first because it is the most likely source of silent error in the whole system (weekends, DST switches, the 09:15/12:15/18:30 boundaries).

Key decisions (full rationale in `docs/adr/`):

- **`core/` never imports `homeassistant`** — this is what lets the same logic run under pytest, in the backtest, and in production.
- **Planner, not driver (ADR-0004):** the battery accepts one Modbus connection and `marstek_venus_modbus` owns it. All actuation goes through `hass.services.async_call` against that integration's entities. Never open Modbus directly, not even for a read.
- **Tariff calendar versioned by effective date (ADR-0005):** `CALENDARS = [(effective_date, table), ...]`. Never hardcode the current year — ERSE revises periods annually and a 2027 reform is expected.
- **Own greedy optimiser in v1 (ADR-0003);** EMHASS/LP only reconsidered if the backtest shows >€10/year headroom.

## Hard rules

The invariants in `CONTEXT.md` §Invariants are non-negotiable (zero-export, contracted-power ceiling, 27% planning reserve floor — run-time floor enforcement is the battery's per ADR-0008, no simultaneous charge+discharge, single Modbus connection, versioned calendar, HA-free core). Violating one is a bug, not a configuration choice. Spec §11 additionally lists "ask first" items: lowering the reserve floor, raising contracted power, extra cycles into cheias.

Constants (K1, TAR values, battery parameters, wear cost) are defined once in `CONTEXT.md` and sourced from `docs/tariff-reference.md` — do not invent or "correct" tariff values; they are verifiable against ERSE/EDP documents.

Portuguese period names (`ponta`, `cheias`, `vazio`) stay untranslated in code and docs — they are regulatory terms, and translating them breaks cross-checking against ERSE/EDP sources.

The plan has human-review checkpoints (A–D in `docs/plan.md`); do not proceed past one without the review it calls for. Checkpoint B is a go/no-go: if the dynamic optimiser backtests below €30/year over the static baseline, the correct outcome is to stop.
