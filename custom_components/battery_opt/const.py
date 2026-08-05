"""
Constants for the battery_opt integration shell.

Domain constants only — tariff and battery physics live in `core/`
(CONTEXT.md is their single source of truth). Defaults here mirror
CONTEXT.md and the Checkpoint B decisions (docs/findings.md):
plan-wear 0.0467 is the chosen cheias-cycling cap.
"""

DOMAIN = "battery_opt"

CONF_MODE_SELECT = "mode_select"
CONF_POWER_NUMBER = "power_number"
CONF_SOC_SENSOR = "soc_sensor"
CONF_PRICE_SENSOR = "price_sensor"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_RESERVE_FLOOR_PCT = "reserve_floor_pct"
CONF_WEAR_COST = "wear_cost_eur_kwh"
CONF_PLAN_WEAR = "plan_wear_eur_kwh"

DEFAULT_CAPACITY_KWH = 5.0
DEFAULT_RESERVE_FLOOR_PCT = 27.0
DEFAULT_WEAR_COST = 0.020
DEFAULT_PLAN_WEAR = 0.0467  # WEAR_COST_MAX; Checkpoint B cycle cap

UPDATE_INTERVAL_MINUTES = 15
