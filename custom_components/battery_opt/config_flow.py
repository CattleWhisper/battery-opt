"""
Config flow for battery_opt.

Collects the marstek_modbus entity ids (ADR-0004: entity ids are
chosen by the user, never hardcoded) and the battery parameters.
Prices need no entity: they come exclusively from HA core's OMIE
integration via its get_prices_for_date service, whose presence is
validated here. Everything is editable afterwards through the
options flow — including adding the battery entities when the
battery arrives; the entry reloads on save.

Note on the reserve floor: 27% is the documented default and spec §11
lists lowering it as an ask-first action — the form allows it because
the owner filling the form IS the asker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_CUTOFF_NUMBER,
    CONF_CHARGE_POWER_NUMBER,
    CONF_CHARGE_TO_SOC_NUMBER,
    CONF_DISCHARGE_CUTOFF_NUMBER,
    CONF_DRY_RUN,
    CONF_GRID_ENERGY_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_MODE_SELECT,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_RS485_SWITCH,
    CONF_SELF_DISCHARGE_W,
    CONF_WEAR_COST,
    CONF_WORK_MODE_SELECT,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_DRY_RUN,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_SELF_DISCHARGE_W,
    DEFAULT_WEAR_COST,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def _entity(domain: str) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain),
    )


def _number(
    minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        ),
    )


def _parameter_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Numeric parameters, shared by setup and options."""
    return {
        vol.Required(
            CONF_CAPACITY_KWH,
            default=defaults.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH),
        ): _number(1.0, 30.0, 0.01, "kWh"),
        vol.Required(
            CONF_RESERVE_FLOOR_PCT,
            default=defaults.get(CONF_RESERVE_FLOOR_PCT, DEFAULT_RESERVE_FLOOR_PCT),
        ): _number(5.0, 80.0, 1.0, "%"),
        vol.Required(
            CONF_WEAR_COST,
            default=defaults.get(CONF_WEAR_COST, DEFAULT_WEAR_COST),
        ): _number(0.0, 0.10, 0.001, "EUR/kWh"),
        vol.Required(
            CONF_PLAN_WEAR,
            default=defaults.get(CONF_PLAN_WEAR, DEFAULT_PLAN_WEAR),
        ): _number(0.0, 0.30, 0.001, "EUR/kWh"),
        # Measured standby drain (owner 2026-08-17, ~19 W): shapes the
        # published SoC trajectories and the day-chaining seeds only.
        vol.Required(
            CONF_SELF_DISCHARGE_W,
            default=defaults.get(CONF_SELF_DISCHARGE_W, DEFAULT_SELF_DISCHARGE_W),
        ): _number(0.0, 100.0, 1.0, "W"),
        # Task 12: ON (default) = executor actuates the static plan,
        # greedy stays advisory; OFF = greedy actuation with static
        # fallback. Flip only after the Checkpoint C review.
        vol.Required(
            CONF_DRY_RUN,
            default=defaults.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
        ): selector.BooleanSelector(),
    }


# Battery entities are optional as a group: absent until the battery
# arrives (planning-only mode), all four once it does (ADR-0006: the
# rs485 switch and work-mode select carry the state transitions;
# discharge power is gone — discharge is a mode switch, never a
# setpoint; the SoC sensor is gone too — the reserve floor is the
# battery's to manage, owner decision 2026-08-07).
BATTERY_ENTITY_KEYS = (
    CONF_MODE_SELECT,
    CONF_CHARGE_POWER_NUMBER,
    CONF_RS485_SWITCH,
    CONF_WORK_MODE_SELECT,
)


def _entity_schema(current: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Entity pickers, pre-filled with current values when editing."""

    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value else {}

    return {
        vol.Optional(CONF_MODE_SELECT, description=suggested(CONF_MODE_SELECT)): (
            _entity("select")
        ),
        vol.Optional(
            CONF_CHARGE_POWER_NUMBER,
            description=suggested(CONF_CHARGE_POWER_NUMBER),
        ): _entity("number"),
        vol.Optional(CONF_RS485_SWITCH, description=suggested(CONF_RS485_SWITCH)): (
            _entity("switch")
        ),
        vol.Optional(
            CONF_WORK_MODE_SELECT, description=suggested(CONF_WORK_MODE_SELECT)
        ): _entity("select"),
        # Optional control extras (spec §8): the charge-to-SoC backstop
        # and the setup-time cutoff writes activate only when their
        # entity is configured — the cutoff numbers are MISSING on the
        # Venus E V3 upstream register map, so leaving them empty is
        # the expected state there.
        vol.Optional(
            CONF_CHARGE_TO_SOC_NUMBER,
            description=suggested(CONF_CHARGE_TO_SOC_NUMBER),
        ): _entity("number"),
        vol.Optional(
            CONF_CHARGE_CUTOFF_NUMBER,
            description=suggested(CONF_CHARGE_CUTOFF_NUMBER),
        ): _entity("number"),
        vol.Optional(
            CONF_DISCHARGE_CUTOFF_NUMBER,
            description=suggested(CONF_DISCHARGE_CUTOFF_NUMBER),
        ): _entity("number"),
        # Meter entities (plan Tasks 11/13): optional, independent of
        # the battery entities above. Unset -> flat load forecast and
        # an unavailable cost sensor; nothing else changes.
        vol.Optional(CONF_LOAD_SENSOR, description=suggested(CONF_LOAD_SENSOR)): (
            _entity("sensor")
        ),
        vol.Optional(
            CONF_GRID_ENERGY_SENSOR, description=suggested(CONF_GRID_ENERGY_SENSOR)
        ): _entity("sensor"),
        # Charge-power loop inputs (ADR-0007, Task 15): grid-import
        # power (W) and the battery's own power (W). Both needed for
        # the loop; until both are set, CHARGE uses the conservative
        # static fallback setpoint.
        vol.Optional(
            CONF_GRID_POWER_SENSOR, description=suggested(CONF_GRID_POWER_SENSOR)
        ): _entity("sensor"),
        vol.Optional(
            CONF_BATTERY_POWER_SENSOR,
            description=suggested(CONF_BATTERY_POWER_SENSOR),
        ): _entity("sensor"),
    }


# The tariff calendar, the OMIE market day and every wall-clock
# trigger are Portugal-local. Rather than half-defending against a
# foreign HA timezone (the pre-fix code converted in _quarter_index
# but trusted hass everywhere else), the integration requires it.
REQUIRED_TIME_ZONE = "Europe/Lisbon"


def _validate(hass: Any, merged: dict[str, Any]) -> dict[str, str]:
    """Shared validation: OMIE set up, Lisbon timezone, battery all-or-none."""
    errors: dict[str, str] = {}
    if not hass.services.has_service("omie", "get_prices_for_date"):
        # Prices come exclusively from HA core's OMIE integration.
        errors["base"] = "omie_not_set_up"
        return errors
    if hass.config.time_zone != REQUIRED_TIME_ZONE:
        errors["base"] = "timezone_not_lisbon"
        return errors
    provided = sum(1 for key in BATTERY_ENTITY_KEYS if merged.get(key))
    if provided not in (0, len(BATTERY_ENTITY_KEYS)):
        errors["base"] = "battery_entities_all_or_none"
    return errors


class BatteryOptConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: entities plus battery parameters."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect everything in a single form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="Battery Opt", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({**_entity_schema({}), **_parameter_schema({})}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004 - HA-required signature
    ) -> BatteryOptOptionsFlow:
        """Expose entities and parameters for later editing."""
        return BatteryOptOptionsFlow()


class BatteryOptOptionsFlow(OptionsFlow):
    """
    Edit entities and numeric parameters after setup.

    The effective configuration is entry.data overlaid with these
    options; __init__ reloads the entry on change, so switching the
    price sensor or adding the battery entities when the battery
    arrives requires no remove-and-re-add.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the form pre-filled with current values."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate the POST-SAVE effective config: saving REPLACES
            # the options with user_input, so the effective config is
            # data + user_input — not data + old options + user_input.
            # Merging `current` here once let a cleared battery entity
            # pass all-or-none while silently dropping to planning-only.
            merged = {**self.config_entry.data, **user_input}
            errors = _validate(self.hass, merged)
            if not errors:
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {**_entity_schema(current), **_parameter_schema(current)}
            ),
            errors=errors,
        )
