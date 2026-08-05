# ADR-0004: The integration is a planner, not a driver

**Status:** Accepted · **Date:** 2026-08

## Context

The Marstek Venus E accepts **one Modbus connection at a time**. The `marstek_venus_modbus` integration (HACS) already owns that connection and exposes read and write entities.

The usual pattern is for an integration to own its device. Here that would mean replacing the existing integration or contending for the connection.

## Decision

This integration **never opens a Modbus connection**. It actuates exclusively through service calls against `marstek_venus_modbus` entities:

```python
await hass.services.async_call(
    "number", "set_value",
    {"entity_id": "number.marstek_charge_power", "value": watts},
)
```

## Rationale

- Two simultaneous Modbus connections fail. This is not a matter of style.
- The planner/driver boundary is cleaner: firmware or register-map changes become the upstream integration's problem.
- It allows the whole executor to be tested against a fake driver, with no hardware.
- If `marstek_venus_modbus` becomes unmaintained, the driver implementation can be replaced without touching the planner.

## Consequences

- Dependency on a third-party integration and on the stability of its entity names. Mitigated by config flow: entity IDs are chosen by the user, not hardcoded.
- Higher latency than direct Modbus writes. Irrelevant at a 15-minute cadence.
- If the upstream integration is down, the health binary sensor goes off and we do not actuate — the correct behaviour.
- **Never** bypass this, not even for a one-off read.
