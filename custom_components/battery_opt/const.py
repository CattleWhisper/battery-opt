"""
Constants for the battery_opt integration shell.

Domain constants only — tariff and battery physics live in `core/`
(CONTEXT.md is their single source of truth). Defaults here mirror
CONTEXT.md and the Checkpoint B decisions (docs/findings.md):
plan-wear 0.0467 is the chosen cheias-cycling cap.
"""

DOMAIN = "battery_opt"

CONF_MODE_SELECT = "mode_select"
CONF_CHARGE_POWER_NUMBER = "charge_power_number"
CONF_SOC_SENSOR = "soc_sensor"
# ADR-0006 control entities: rs485 switch + work-mode select complete
# the required battery group; the three numbers are optional-with-
# graceful-degradation (no backstop / no setup cutoff writes). The
# cutoff numbers are MISSING on the Venus E V3 upstream register map.
CONF_RS485_SWITCH = "rs485_switch"
CONF_WORK_MODE_SELECT = "work_mode_select"
CONF_CHARGE_TO_SOC_NUMBER = "charge_to_soc_number"
CONF_CHARGE_CUTOFF_NUMBER = "charge_cutoff_number"
CONF_DISCHARGE_CUTOFF_NUMBER = "discharge_cutoff_number"
CONF_PRICE_SENSOR = "price_sensor"  # legacy: pre-core-OMIE entries carry it in data
# legacy: pre-ADR-0006 entries carry it; discharge is a mode switch now
CONF_DISCHARGE_POWER_NUMBER = "discharge_power_number"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_RESERVE_FLOOR_PCT = "reserve_floor_pct"
CONF_WEAR_COST = "wear_cost_eur_kwh"
CONF_PLAN_WEAR = "plan_wear_eur_kwh"
# Both optional, both meter entities (plan Tasks 11/13, decision 1):
# owner picks them once the meter is known. Everything degrades
# gracefully when unset — flat load, cost sensor unavailable. Kept as
# two separate keys even though this house has no solar and they will
# likely point at the same entity.
CONF_LOAD_SENSOR = "load_sensor"  # house consumption: power W or energy kWh
CONF_GRID_ENERGY_SENSOR = "grid_energy_sensor"  # grid-import energy kWh

DEFAULT_CAPACITY_KWH = 5.0
DEFAULT_RESERVE_FLOOR_PCT = 27.0
DEFAULT_WEAR_COST = 0.020
DEFAULT_PLAN_WEAR = 0.0467  # WEAR_COST_MAX; Checkpoint B cycle cap

UPDATE_INTERVAL_MINUTES = 15

BASE_LOAD_W = 1040.0  # CONTEXT.md: flat 24/7 load; Task 11 adds forecasting
