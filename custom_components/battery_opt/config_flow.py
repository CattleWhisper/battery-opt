"""
Config flow for battery_opt.

The parent entry collects the house-level pieces: meter entities and
the planning parameters. Each battery of the fleet is a `battery`
SUBENTRY (ADR-0009, Task 16) carrying its own marstek_modbus entity
ids (ADR-0004: chosen by the user, never hardcoded), its power sensor
and its physical parameters — added or reconfigured from the
integration page at any time; the entry reloads on change. Prices
need no entity: they come exclusively from HA core's OMIE integration
via its get_prices_for_date service, whose presence is validated here
whenever the tariff is indexed.

ADR-0012: the supply contract is three selectors on the main form
(pricing, option, cycle — defaulting to the pre-ADR-0012 contract), and
a fixa contract adds one step for its prices and daily fixed term.

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
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
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
    CONF_FIXED_EUR_DAY,
    CONF_GRID_ENERGY_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_MODE_SELECT,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_RS485_SWITCH,
    CONF_SELF_DISCHARGE_W,
    CONF_SOC_SENSOR,
    CONF_TARIFF_CYCLE,
    CONF_TARIFF_OPTION,
    CONF_TARIFF_PRICING,
    CONF_WEAR_COST,
    CONF_WORK_MODE_SELECT,
    DEFAULT_CAPACITY_KWH,
    DEFAULT_DRY_RUN,
    DEFAULT_PLAN_WEAR,
    DEFAULT_RESERVE_FLOOR_PCT,
    DEFAULT_SELF_DISCHARGE_W,
    DEFAULT_TARIFF_CYCLE,
    DEFAULT_TARIFF_OPTION,
    DEFAULT_TARIFF_PRICING,
    DEFAULT_WEAR_COST,
    DOMAIN,
    FIXA_PRICE_KEYS,
    SUBENTRY_TYPE_BATTERY,
)
from .tariff_config import CYCLES, OPTIONS, PRICINGS, tariff_axes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def _entity(domain: str) -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain),
    )


def _number(
    minimum: float, maximum: float, step: float | str, unit: str | None = None
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


def _select(options: tuple[str, ...], translation_key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(options),
            translation_key=translation_key,
            mode=selector.SelectSelectorMode.DROPDOWN,
        ),
    )


def _tariff_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Return the supply contract's three axes (ADR-0012), for setup and options."""
    return {
        vol.Required(
            CONF_TARIFF_PRICING,
            default=defaults.get(CONF_TARIFF_PRICING, DEFAULT_TARIFF_PRICING),
        ): _select(PRICINGS, CONF_TARIFF_PRICING),
        vol.Required(
            CONF_TARIFF_OPTION,
            default=defaults.get(CONF_TARIFF_OPTION, DEFAULT_TARIFF_OPTION),
        ): _select(OPTIONS, CONF_TARIFF_OPTION),
        # Ignored for simples (one price group all day).
        vol.Required(
            CONF_TARIFF_CYCLE,
            default=defaults.get(CONF_TARIFF_CYCLE, DEFAULT_TARIFF_CYCLE),
        ): _select(CYCLES, CONF_TARIFF_CYCLE),
    }


def _tariff_prices_schema(option: str, defaults: dict[str, Any]) -> vol.Schema:
    """Return a fixa contract's price fields for its option, plus its fixed term."""

    def field(key: str) -> vol.Required:
        value = defaults.get(key)
        return vol.Required(key) if value is None else vol.Required(key, default=value)

    schema: dict[vol.Marker, Any] = {
        # "any": EDP prints prices to 4 decimals, below HA's step floor.
        field(key): _number(0.0, 2.0, "any", "EUR/kWh")
        for key in FIXA_PRICE_KEYS[option]
    }
    schema[field(CONF_FIXED_EUR_DAY)] = _number(0.0, 5.0, "any", "EUR/day")
    return vol.Schema(schema)


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


def _entity_schema(current: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Entity pickers, pre-filled with current values when editing."""

    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value else {}

    return {
        # Meter entities (plan Tasks 11/13): optional. Unset -> flat
        # load forecast and an unavailable cost sensor; nothing else
        # changes. Batteries are NOT here: each one is a `battery`
        # subentry (ADR-0009, Task 16) added from the integration page.
        vol.Optional(CONF_LOAD_SENSOR, description=suggested(CONF_LOAD_SENSOR)): (
            _entity("sensor")
        ),
        vol.Optional(
            CONF_GRID_ENERGY_SENSOR, description=suggested(CONF_GRID_ENERGY_SENSOR)
        ): _entity("sensor"),
        # Charge-power loop input (ADR-0007, Task 15): grid-import
        # power (W). The loop also needs every battery subentry's own
        # power sensor; until all are set, CHARGE uses the
        # conservative static fallback setpoint.
        vol.Optional(
            CONF_GRID_POWER_SENSOR, description=suggested(CONF_GRID_POWER_SENSOR)
        ): _entity("sensor"),
    }


def _battery_schema(current: dict[str, Any]) -> vol.Schema:
    """One battery subentry's form (ADR-0009): entities + physicals."""

    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value else {}

    return vol.Schema(
        {
            vol.Required(CONF_MODE_SELECT, description=suggested(CONF_MODE_SELECT)): (
                _entity("select")
            ),
            vol.Required(
                CONF_CHARGE_POWER_NUMBER,
                description=suggested(CONF_CHARGE_POWER_NUMBER),
            ): _entity("number"),
            vol.Required(
                CONF_RS485_SWITCH, description=suggested(CONF_RS485_SWITCH)
            ): _entity("switch"),
            vol.Required(
                CONF_WORK_MODE_SELECT, description=suggested(CONF_WORK_MODE_SELECT)
            ): _entity("select"),
            # Optional control extras (spec §8): the charge-to-SoC
            # backstop and the setup-time cutoff writes activate only
            # when their entity is configured — the cutoff numbers are
            # MISSING on the Venus E V3 upstream register map, so
            # leaving them empty is the expected state there.
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
            # This unit's own power (W, HA battery convention): the
            # ADR-0007 loop and the realised tracker need the COMPLETE
            # fleet draw, so every unit should contribute one.
            vol.Optional(
                CONF_BATTERY_POWER_SENSOR,
                description=suggested(CONF_BATTERY_POWER_SENSOR),
            ): _entity("sensor"),
            # This unit's SoC (%), ADR-0010. Read at most once a day,
            # just after midnight, purely to seed that day's plan from
            # where the battery really is. Optional: leave it empty and
            # every seed stays the modelled chain (pre-ADR-0010
            # behaviour). Like the power sensor, EVERY unit must set it
            # for the fleet total to be usable.
            vol.Optional(
                CONF_SOC_SENSOR,
                description=suggested(CONF_SOC_SENSOR),
            ): _entity("sensor"),
            # Per-unit physicals (ADR-0009): summed in code.
            vol.Required(
                CONF_CAPACITY_KWH,
                default=current.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH),
            ): _number(1.0, 30.0, 0.01, "kWh"),
            vol.Required(
                CONF_SELF_DISCHARGE_W,
                default=current.get(CONF_SELF_DISCHARGE_W, DEFAULT_SELF_DISCHARGE_W),
            ): _number(0.0, 100.0, 1.0, "W"),
        }
    )


# The tariff calendar, the OMIE market day and every wall-clock
# trigger are Portugal-local. Rather than half-defending against a
# foreign HA timezone (the pre-fix code converted in _quarter_index
# but trusted hass everywhere else), the integration requires it.
REQUIRED_TIME_ZONE = "Europe/Lisbon"


def _validate(hass: Any, merged: dict[str, Any]) -> dict[str, str]:
    """Shared validation: OMIE set up (indexed tariffs), Lisbon timezone."""
    errors: dict[str, str] = {}
    pricing, _option, _cycle = tariff_axes(merged)
    if pricing != "fixa" and not hass.services.has_service(
        "omie", "get_prices_for_date"
    ):
        # Prices come exclusively from HA core's OMIE integration.
        errors["base"] = "omie_not_set_up"
        return errors
    if hass.config.time_zone != REQUIRED_TIME_ZONE:
        errors["base"] = "timezone_not_lisbon"
    return errors


class BatteryOptConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: house-level entities plus planning parameters."""

    # v2 (ADR-0009, Task 16): batteries are subentries; the v1 flat
    # entity group migrates in __init__.async_migrate_entry.
    # v3 (ADR-0012): the tariff parameter.
    VERSION = 3

    def __init__(self) -> None:
        """Hold the main form while a fixa contract's prices are asked."""
        super().__init__()
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect the house-level form; batteries are subentries."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(self.hass, user_input)
            if not errors:
                if user_input.get(CONF_TARIFF_PRICING) == "fixa":
                    self._data = dict(user_input)
                    return await self.async_step_tariff_prices()
                return self.async_create_entry(title="Battery Opt", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    **_entity_schema({}),
                    **_tariff_schema({}),
                    **_parameter_schema({}),
                }
            ),
            errors=errors,
        )

    async def async_step_tariff_prices(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Fixa only (ADR-0012): the contract's prices and daily fixed term."""
        if user_input is not None:
            return self.async_create_entry(
                title="Battery Opt", data={**self._data, **user_input}
            )
        return self.async_show_form(
            step_id="tariff_prices",
            data_schema=_tariff_prices_schema(
                self._data[CONF_TARIFF_OPTION], self._data
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004 - HA-required signature
    ) -> BatteryOptOptionsFlow:
        """Expose entities and parameters for later editing."""
        return BatteryOptOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003 - HA-required signature
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Each battery of the fleet is a subentry (ADR-0009)."""
        return {SUBENTRY_TYPE_BATTERY: BatterySubentryFlowHandler}


class BatterySubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure one battery of the fleet (ADR-0009)."""

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Add a battery: its entities and physical parameters."""
        if user_input is not None:
            existing = sum(
                1
                for subentry in self._get_entry().subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_BATTERY
            )
            return self.async_create_entry(
                title=f"Battery {existing + 1}", data=user_input
            )
        return self.async_show_form(step_id="user", data_schema=_battery_schema({}))

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Edit an existing battery, pre-filled with current values."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(), subentry, data=user_input
            )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_battery_schema(dict(subentry.data))
        )


class BatteryOptOptionsFlow(OptionsFlow):
    """
    Edit entities and numeric parameters after setup.

    The effective configuration is entry.data overlaid with these
    options; __init__ reloads the entry on change, so switching the
    price sensor or adding the battery entities when the battery
    arrives requires no remove-and-re-add.
    """

    def __init__(self) -> None:
        """Hold the main form while a fixa contract's prices are asked."""
        super().__init__()
        self._options: dict[str, Any] = {}

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
                if user_input.get(CONF_TARIFF_PRICING) == "fixa":
                    self._options = dict(user_input)
                    return await self.async_step_tariff_prices()
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_entity_schema(current),
                    **_tariff_schema(current),
                    **_parameter_schema(current),
                }
            ),
            errors=errors,
        )

    async def async_step_tariff_prices(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Fixa only (ADR-0012): prices pre-filled with the current ones."""
        if user_input is not None:
            return self.async_create_entry(data={**self._options, **user_input})
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="tariff_prices",
            data_schema=_tariff_prices_schema(
                self._options[CONF_TARIFF_OPTION], current
            ),
        )
