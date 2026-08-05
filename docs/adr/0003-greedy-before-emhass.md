# ADR-0003: Own greedy optimiser before considering EMHASS

**Status:** Accepted · **Date:** 2026-08 · **Revisit:** Checkpoint B

## Context

EMHASS (Energy Management for Home Assistant) solves the same problem with linear programming (CVXPY + HiGHS), and its parameters map almost one-to-one onto this project's constraints: `maximum_power_to_grid: 0` for zero-export, `maximum_power_from_grid` for contracted power, `battery_minimum_percent` for the reserve floor, and `weight_battery_charge`/`weight_battery_discharge` for the wear cost. It accepts `load_cost_forecast` as an input, so a three-tier regulated tariff stacked on a market index is fully representable.

## Decision

Implement an **own greedy** in v1. Reassess EMHASS at Checkpoint B, with backtest numbers in hand.

## Rationale

- **Phase 0 is a backtest, and EMHASS is built for live operation.** Replaying 12 months means driving its REST API day by day. The greedy runs offline in seconds.
- The greedy is ~40 lines. With one battery and 96 intervals, the gap to optimal is typically <2% — around €10/year against a total gain of €30–80.
- EMHASS adds a container, a REST API and a version-compatibility surface. Its core was recently rewritten (PuLP to CVXPY); expect breaking changes.
- The genuinely project-specific work — calendar, price formula, OMIE ingestion, driver, reconciliation — is required either way. EMHASS covers ~40% of the spec; the other 60% is ours regardless.
- EMHASS's deferrable-load model (washing machine, EV charger) does not apply: our load is flat and inflexible.

## Consequences

- If the backtest shows the greedy losing **>€10/year** against optimal, adopt EMHASS in Phase 2, or implement an LP with `pulp`/`scipy`.
- An LP inside the integration requires `async_add_executor_job` (see ADR-0002).
- EMHASS advantages left unexploited until then: 48 h horizon (which would resolve the open question on end-of-day SoC target), MPC mode, and maintained load forecasting.
