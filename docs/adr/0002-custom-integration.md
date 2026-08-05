# ADR-0002: Custom integration, not pyscript or AppDaemon

**Status:** Accepted · **Date:** 2026-08

## Context

Three ways to run non-trivial Python in Home Assistant: pyscript (HACS), AppDaemon (separate container), or a custom integration.

## Decision

**Custom integration**, installable via HACS.

## Rationale

| Criterion | pyscript | AppDaemon | Integration |
|---|---|---|---|
| Interpreter | **own AST-based, not CPython** | CPython | **CPython** |
| Dependencies | manual | container `pip` | `manifest.json` |
| Configuration | YAML | YAML | **config flow (UI)** |
| Entities | pseudo-entities | via API | **real, with `unique_id` and restore** |
| Tests | pytest, **different interpreter** | pytest, identical | **pytest, identical** |
| Distribution | copy files | copy files | **versioned via HACS** |

The first row decides it. pyscript implements its own interpreter on top of `ast` — close to CPython, but not identical. Testing in CPython and running on a different interpreter introduces exactly the class of divergence that surfaces as a wrong period at 09:15 on a Saturday.

The cost is boilerplate (`manifest.json`, `config_flow.py`, `coordinator.py`, `strings.json`) and slower iteration — no hot reload. Acceptable: by the time the integration is written there is no first draft to produce, only a tested package to wrap.

## Consequences

- Everything runs on HA's event loop. Any blocking operation (HTTP, file I/O, solver) needs `hass.async_add_executor_job`. The greedy at 96 intervals is safe inline; an LP would not be.
- Exposure to HA API changes. Mitigated by using only `DataUpdateCoordinator`, `SensorEntity` and `hass.services.async_call` — among the most stable.
- Phase 0 of the plan needs none of this. It is a package, pytest and a CSV.
