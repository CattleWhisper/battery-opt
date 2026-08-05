"""
Config flow for battery_opt.

Collects the marstek_venus_modbus entity ids (ADR-0004: entity ids are
chosen by the user, never hardcoded), the OMIE price entity, and the
battery parameters. The numeric parameters are editable afterwards
through the options flow; changing entities means re-adding the
integration.

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
    CONF_CAPACITY_KWH,
    CONF_MODE_SELECT,
    CONF_PLAN_WEAR,
    CONF_POWER_NUMBER,
    CONF_PRICE_SENSOR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_SOC_SENSOR,
    CONF_WEAR_COST,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
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
    }


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODE_SELECT): _entity("select"),
        vol.Required(CONF_POWER_NUMBER): _entity("number"),
        vol.Required(CONF_SOC_SENSOR): _entity("sensor"),
        vol.Required(CONF_PRICE_SENSOR): _entity("sensor"),
        **_parameter_schema({}),
    }
)


class BatteryOptConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: entities plus battery parameters."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect everything in a single form."""
        if user_input is not None:
            return self.async_create_entry(title="Battery Opt", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004 - HA-required signature
    ) -> BatteryOptOptionsFlow:
        """Expose the numeric parameters for later editing."""
        return BatteryOptOptionsFlow()


class BatteryOptOptionsFlow(OptionsFlow):
    """Edit the numeric parameters after setup."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the parameter form pre-filled with current values."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_parameter_schema(current)),
        )
