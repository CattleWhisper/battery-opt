"""
Config flow and entry setup tests for battery_opt.

Runs under pytest-homeassistant-custom-component: `hass` is the real
Home Assistant test harness. Prices come exclusively from HA core's
OMIE integration, stubbed here as the real `omie.get_prices_for_date`
service with a response in the shape verified from
home-assistant/core sources.
"""

import itertools
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

from custom_components.battery_opt.const import (
    BASE_LOAD_W,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_POWER_NUMBER,
    CONF_GRID_ENERGY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_MODE_SELECT,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_RS485_SWITCH,
    CONF_WEAR_COST,
    CONF_WORK_MODE_SELECT,
    DOMAIN,
)
from custom_components.battery_opt.core.prices import price
from custom_components.battery_opt.core.static_schedule import chained_start_soc

BATTERY_ENTITIES = {
    CONF_MODE_SELECT: "select.marstek_force_mode",
    CONF_CHARGE_POWER_NUMBER: "number.marstek_set_charge_power",
    CONF_RS485_SWITCH: "switch.marstek_rs485_control_mode",
    CONF_WORK_MODE_SELECT: "select.marstek_user_work_mode",
}

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}

VALID_INPUT = {**BATTERY_ENTITIES, **PARAMETERS}


# Mirrors SERVICE_GET_PRICES_SCHEMA in home-assistant/core: `countries`
# coerces to the Country enum, whose values are LOWERCASE "es"/"pt".
# Sending "PT" must fail here exactly as it does in production.
_OMIE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Required("countries", default=["es", "pt"]): vol.All(
            cv.ensure_list, [vol.In(["es", "pt"])]
        ),
    }
)


def _register_core_omie_service(
    hass: HomeAssistant,
    days_available: int = 2,
    calls: list | None = None,
) -> None:
    """Stub HA core's omie.get_prices_for_date service."""
    cet = ZoneInfo("Europe/Madrid")
    first_served = dt_util.now().date()

    async def handler(call: ServiceCall) -> dict:
        if calls is not None:
            calls.append(dict(call.data))
        market_date = call.data["date"]
        if (market_date - first_served).days >= days_available:
            msg = "data_not_available"
            raise ServiceValidationError(msg)
        midnight = datetime(
            market_date.year, market_date.month, market_date.day, tzinfo=cet
        )
        return {
            country: [
                {
                    "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
                    "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "price": 0.06,
                }
                for i in range(96)
            ]
            for country in call.data["countries"]
            if country == "pt"
        }

    hass.services.async_register(
        "omie",
        "get_prices_for_date",
        handler,
        schema=_OMIE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _set_lisbon(hass: HomeAssistant) -> None:
    """Flows validate the HA timezone; the test default is US/Pacific."""
    await hass.config.async_set_time_zone("Europe/Lisbon")


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The single-step form creates a config entry with the input."""
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VALID_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Battery Opt"
    # Task 12: the schema defaults dry_run ON — greedy stays advisory.
    # The measured standby drain (owner 2026-08-17) defaults to 19 W.
    assert result["data"] == {**VALID_INPUT, "dry_run": True, "self_discharge_w": 19.0}


async def test_flow_errors_when_omie_not_set_up(hass: HomeAssistant) -> None:
    """Without HA core's OMIE integration there is no price source."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(PARAMETERS)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "omie_not_set_up"}


async def test_flow_rejects_partial_battery_entities(hass: HomeAssistant) -> None:
    """One battery entity without the others is a form error."""
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    partial = {
        key: value
        for key, value in VALID_INPUT.items()
        if key not in (CONF_CHARGE_POWER_NUMBER, CONF_RS485_SWITCH)
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], partial)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "battery_entities_all_or_none"}


async def test_flow_rejects_non_lisbon_timezone(hass: HomeAssistant) -> None:
    """
    HA's timezone must be Europe/Lisbon (the test default is US/Pacific).

    The tariff calendar, the OMIE market day and every trigger are
    Portugal-local; the flow refuses rather than half-defending.
    """
    _register_core_omie_service(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(PARAMETERS)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "timezone_not_lisbon"}


async def test_setup_entry_with_battery_loads(
    hass: HomeAssistant,
) -> None:
    """
    A full battery config loads; params come from the entry.

    No SoC is read anywhere (owner decision 2026-08-07): the entry
    loads with no Marstek state present at all.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.planning_only
    assert entry.runtime_data.executor is not None
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.35)
    assert coordinator.battery_params.self_discharge_w == pytest.approx(19.0)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.0467)


async def test_options_flow_edits_parameters(hass: HomeAssistant) -> None:
    """Numeric parameters are editable afterwards and take effect."""
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CAPACITY_KWH: 5.0,
            CONF_RESERVE_FLOOR_PCT: 30.0,
            CONF_WEAR_COST: 0.025,
            CONF_PLAN_WEAR: 0.06,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.battery_params.cap_min_kwh == pytest.approx(1.5)
    assert coordinator.battery_params.wear_cost_eur_kwh == pytest.approx(0.025)
    assert coordinator.plan_wear_eur_kwh == pytest.approx(0.06)


async def test_meter_sensors_are_optional_and_editable_later(
    hass: HomeAssistant,
) -> None:
    """
    Plan Tasks 11/13, decision 1: two independent optional meter keys.

    An entry created without them reloads cleanly (no new required
    field); the options flow can add them later, same as the battery
    entities.
    """
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **PARAMETERS,
            CONF_LOAD_SENSOR: "sensor.house_power",
            CONF_GRID_ENERGY_SENSOR: "sensor.grid_import_energy",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    merged = {**entry.data, **entry.options}
    assert merged[CONF_LOAD_SENSOR] == "sensor.house_power"
    assert merged[CONF_GRID_ENERGY_SENSOR] == "sensor.grid_import_energy"


async def test_planning_only_computes_plan_from_core_omie(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    No battery: the entry loads and the advisory plan is computed.

    The battery entities are simply absent; the coordinator pulls the
    day series from the OMIE service and publishes the capped-greedy
    plan with nothing to actuate. Frozen to a winter weekday so the
    day-chained seed provably equals the reserve floor (the previous
    winter weekday ends drained) — a summer date would seed full and
    a weekend date would vary by which weekday it chains from.
    """
    freezer.move_to("2026-01-15T12:00:00+00:00")  # Thursday, winter
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.executor is None

    coordinator = entry.runtime_data.coordinator
    assert coordinator.planning_only
    assert coordinator.data["prices_ok"] is True
    assert coordinator.data["prices_padded"] is False
    assert len(coordinator.data["plan_charge_w"]) == 96
    assert coordinator.data["forecast_saving_eur"] is not None
    assert coordinator.data["vs_static_eur"] is not None

    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.attributes["mode"] == "planning_only"
    assert plan_state.state in ("charge", "discharge", "hold")
    savings = hass.states.get("sensor.battery_opt_forecast_savings")
    assert savings.state not in ("unknown", "unavailable")
    assert hass.states.get("sensor.battery_opt_vs_static") is not None
    healthy = hass.states.get("binary_sensor.battery_opt_healthy")
    assert healthy.state == "on"
    assert "planning only" in healthy.attributes["status"]

    # SoC forecast: advisory trajectory, day-chained virtual battery —
    # in winter the chained seed IS the reserve floor.
    soc_forecast = hass.states.get("sensor.battery_opt_soc_forecast")
    assert soc_forecast is not None
    assert soc_forecast.attributes["source"] == "advisory"
    trajectory = soc_forecast.attributes["trajectory_kwh"]
    assert len(trajectory) == 97  # 96 quarters + the midnight boundary
    assert trajectory[0] == pytest.approx(1.35)  # winter chains to the floor
    assert min(trajectory) >= 1.35 - 1e-9  # C-4: never below the floor
    assert max(trajectory) <= 5.0 + 1e-9  # C-5: never above the ceiling
    assert soc_forecast.attributes["trajectory_pct"][0] == pytest.approx(27.0)
    assert 27.0 <= float(soc_forecast.state) <= 100.0


async def test_advisory_trajectory_is_day_chained_in_summer(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Summer advisory SoC starts nearly full, not at the floor.

    The previous weekday's static plan ends full in summer (midday
    charge after the morning ponta), so the chained advisory
    trajectory begins just below 100% — full minus the measured
    standby drain since the fill (owner 2026-08-17) — matching the
    real battery under static actuation, which is the whole point of
    the SoC forecast sensor's forecast-vs-real overlay. The seeded
    greedy can then plan a morning-ponta discharge the floor seed
    made impossible.
    """
    freezer.move_to("2026-07-15T08:00:00+00:00")  # Wednesday, summer
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    soc_forecast = hass.states.get("sensor.battery_opt_soc_forecast")
    coordinator = entry.runtime_data.coordinator
    # Tuesday's charge carried over, minus the standby drain since.
    seed = chained_start_soc(
        date(2026, 7, 15),
        [BASE_LOAD_W] * 96,
        [0.0] * 96,
        coordinator.battery_params,
    )
    assert 4.5 < seed < 5.0
    seed_pct = round(seed / 5.0 * 100.0, 1)
    trajectory = soc_forecast.attributes["trajectory_kwh"]
    assert trajectory[0] == pytest.approx(seed, abs=1e-3)
    assert soc_forecast.attributes["trajectory_pct"][0] == pytest.approx(seed_pct)
    assert max(trajectory) <= 5.0 + 1e-9  # C-5 holds under the full seed
    # Both plans' trajectories ride alongside for the comparison
    # overlay — chained, so each also starts nearly full in summer.
    greedy_pct = soc_forecast.attributes["greedy_trajectory_pct"]
    assert greedy_pct[0] == pytest.approx(seed_pct)
    assert soc_forecast.attributes["static_trajectory_pct"][0] == pytest.approx(
        seed_pct
    )
    # The greedy line is CONTINUOUS across midnight (owner 2026-08-13):
    # tomorrow's preview seeds from today's greedy end, not the static
    # chain — after a full sell-down (~floor) tomorrow starts low and
    # charges overnight, instead of "resetting" to 100%.
    assert len(greedy_pct) == 193  # tomorrow's preview joined
    assert greedy_pct[97] < 60.0  # not the static chain's full reseed
    assert abs(greedy_pct[97] - greedy_pct[96]) < 15.0  # one quarter's step
    # The seeded greedy discharges the morning ponta (09:15-12:15) —
    # flat stub OMIE means the TAR alone makes it profitable.
    plan_state = hass.states.get("sensor.battery_opt_plan")
    morning_discharge = [
        s
        for s in plan_state.attributes["schedule"]
        if s["direction"] == "discharge"
        and s["start"].startswith("2026-07-15")
        and datetime.fromisoformat(s["start"]).hour < 13
    ]
    assert morning_discharge
    # The static baseline rides alongside in the same segment format
    # (the plan-comparison graph): summer static charges 13:00-17:00.
    static_charge = [
        s
        for s in plan_state.attributes["static_schedule"]
        if s["direction"] == "charge" and s["start"].startswith("2026-07-15T13:00")
    ]
    assert static_charge


async def test_greedy_chains_its_own_end_into_the_next_day(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Owner 2026-08-13: today's greedy seeds from yesterday's greedy end.

    Day 1 (summer Wednesday, no prior record) starts at the regime
    default — the static chain's 100% — and sells down to the floor.
    Day 2 must start where day 1 ended, not reset to 100%; and a
    reload mid-day keeps the seed via the persisted record.
    """
    freezer.move_to("2026-07-15T08:00:00+00:00")
    _register_core_omie_service(hass, days_available=3)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    day1 = hass.states.get("sensor.battery_opt_soc_forecast")
    # Regime default: the static chain's near-full seed (minus drain).
    assert day1.attributes["greedy_trajectory_pct"][0] > 90.0
    day1_end_pct = coordinator.data["plan_soc_kwh"][-1] / 5.0 * 100.0

    freezer.move_to("2026-07-16T08:00:00+00:00")  # Thursday
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    day2 = hass.states.get("sensor.battery_opt_soc_forecast")
    start_pct = day2.attributes["greedy_trajectory_pct"][0]
    assert start_pct == pytest.approx(day1_end_pct, abs=0.1)
    assert start_pct == pytest.approx(27.0, abs=1.0)  # the sell-down floor

    # Restart mid-day: the persisted record keeps the chain (and the
    # day's own pinned start, so refreshes never flip the seed).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.states.get("sensor.battery_opt_soc_forecast")
    assert reloaded.attributes["greedy_trajectory_pct"][0] == pytest.approx(
        start_pct, abs=0.1
    )


async def test_current_price_sensor_tracks_the_edp_formula(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    The price sensor carries the delivered price for right now.

    Declared exactly like core OMIE's price sensor (EUR/kWh,
    state_class measurement) so the Energy dashboard accepts it.
    The stub serves a flat 0.06 EUR/kWh spot = 60 EUR/MWh, so the
    expected state is the EDP formula applied to 60 at this instant.

    Time is frozen well inside a single tariff period in both the
    default test hass timezone (US/Pacific) and Europe/Lisbon: without
    this, the assertion is a real wall-clock race between the price
    the sensor snapshots at setup and the fresh price() call below,
    and flakes for real near a period boundary in either timezone.
    """
    freezer.move_to("2026-07-15T20:00:00+00:00")  # Wed 13:00 Pacific / 21:00 Lisbon
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.battery_opt_current_price")
    assert state is not None
    assert float(state.state) == pytest.approx(price(60.0, dt_util.now()))
    assert state.attributes["unit_of_measurement"] == "€/kWh"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["tar_period"] in ("ponta", "cheias", "vazio")
    # Flat stub prices: segments merge to exactly the day's TAR
    # periods, contiguous from local midnight to local midnight.
    segments = state.attributes["prices"]
    assert segments[0]["start"].endswith("T00:00:00+01:00")
    assert segments[-1]["end"].endswith("T00:00:00+01:00")
    assert all(
        first["end"] == second["start"]
        for first, second in itertools.pairwise(segments)
    )


async def test_current_price_and_plan_index_by_lisbon_not_ha_local_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Price/plan sensors must index by Europe/Lisbon, not hass's own timezone.

    Regression test for a real bug: `_quarter_index()` in
    coordinator.py used to read `now.hour`/`now.minute` straight off
    whatever tzinfo `dt_util.now()` carried — that is
    `hass.config.time_zone`, not necessarily Europe/Lisbon — while the
    price/plan vectors are always built on the Lisbon-local calendar
    day (`prices_source._lisbon_date`). Frozen instant is ponta
    (09:15-12:15) in Lisbon but vazio (00:00-07:00) in US/Pacific,
    8h behind in July: with the bug, `sensor.battery_opt_current_price`
    would report the flat-OMIE vazio price (interval 8) instead of the
    correct ponta price (interval 40) — a difference driven entirely
    by the TAR term, since the stub's OMIE spot is flat.
    """
    await hass.config.async_set_time_zone("US/Pacific")
    freezer.move_to("2026-07-15T09:00:00+00:00")  # 10:00 Lisbon / 02:00 Pacific
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    lisbon_now = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Europe/Lisbon"))
    price_state = hass.states.get("sensor.battery_opt_current_price")
    assert float(price_state.state) == pytest.approx(price(60.0, lisbon_now))
    # The price segments are rounded to 5 dp for display; abs tolerance
    # absorbs that, this is still the same slot, not a fresh assertion.
    price_segment = next(
        s
        for s in price_state.attributes["prices"]
        if datetime.fromisoformat(s["start"])
        <= lisbon_now
        < datetime.fromisoformat(s["end"])
    )
    assert price_segment["price_eur_kwh"] == pytest.approx(
        float(price_state.state), abs=1e-4
    )
    assert price_segment["tar_period"] == "ponta"

    plan_state = hass.states.get("sensor.battery_opt_plan")
    plan_segment = next(
        (
            s
            for s in plan_state.attributes["schedule"]
            if datetime.fromisoformat(s["start"])
            <= lisbon_now
            < datetime.fromisoformat(s["end"])
        ),
        None,
    )
    expected_action = plan_segment["direction"] if plan_segment else "hold"
    assert plan_state.state == expected_action


async def test_core_omie_pads_before_tomorrow_publishes(hass: HomeAssistant) -> None:
    """Only market date D available: the final hour pads, flagged."""
    _register_core_omie_service(hass, days_available=1)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["prices_ok"] is True
    assert coordinator.data["prices_padded"] is True


async def test_planning_only_without_omie_is_unhealthy(
    hass: HomeAssistant,
) -> None:
    """OMIE missing at runtime: loaded but unhealthy, no plan."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data["prices_ok"] is False
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "off"
    assert hass.states.get("sensor.battery_opt_forecast_savings").state == "unknown"


async def test_options_flow_adds_battery_entities_later(
    hass: HomeAssistant,
) -> None:
    """
    When the battery arrives, options bring the executor to life.

    Adding the four entities reloads the entry out of planning-only.
    """
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.executor is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BATTERY_ENTITIES, **PARAMETERS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.executor is not None
    assert not entry.runtime_data.coordinator.planning_only


async def test_options_flow_validates_the_post_save_config(
    hass: HomeAssistant,
) -> None:
    """
    Clearing one battery entity in options errors instead of passing.

    Regression: saving REPLACES the options with the form input, so
    validation must run on {data + user_input} — validating against
    the pre-save merged view let a 3-of-4 battery group through,
    silently dropping the entry to planning-only.
    """
    _register_core_omie_service(hass)
    await _set_lisbon(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Battery arrives: all four entities land in the OPTIONS.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BATTERY_ENTITIES, **PARAMETERS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.executor is not None

    # Re-open options and clear ONE battery entity (absent from the
    # submission). The post-save config would hold 3 of 4 → error.
    cleared = {
        key: value
        for key, value in {**BATTERY_ENTITIES, **PARAMETERS}.items()
        if key != CONF_RS485_SWITCH
    }
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], cleared
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "battery_entities_all_or_none"}


async def test_all_entities_group_under_one_service_device(
    hass: HomeAssistant,
) -> None:
    """A single service-type device (like core OMIE's) owns everything."""
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    from homeassistant.helpers.device_registry import DeviceEntryType  # noqa: PLC0415

    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.name == "Battery Opt"

    entity_registry = er.async_get(hass)
    grouped = [e for e in entity_registry.entities.values() if e.device_id == device.id]
    # plan, forecast savings, vs static, price, best periods,
    # SoC forecast, load MAE, cost today, realised savings, healthy,
    # recalculate button
    assert len(grouped) == 11


async def test_recalculate_button_forces_plan_refresh(hass: HomeAssistant) -> None:
    """
    Pressing button.battery_opt_recalculate_plan refreshes NOW.

    The refresh is real, not just wired: the coordinator goes back to
    the OMIE service (today + tomorrow's preview) instead of waiting
    for the 15-minute poll or the 13:45 fetch schedule.
    """
    calls: list = []
    _register_core_omie_service(hass, calls=calls)
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    baseline = len(calls)
    assert baseline > 0  # first refresh already fetched

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.battery_opt_recalculate_plan"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(calls) > baseline
    assert hass.states.get("sensor.battery_opt_plan").state != "unavailable"
    # Apply is active-mode only: no executor here, no button.
    assert hass.states.get("button.battery_opt_apply_plan") is None


async def test_apply_plan_button_ticks_the_executor_now(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Pressing button.battery_opt_apply_plan actuates immediately.

    The press is a REAL executor tick — validation, the override gate
    and the health latch all run — not a bespoke write path. Winter
    Thursday 13:00 Lisbon is cheias → HOLD; from the unknown boot
    state that replays the full ADR-0006 entry: rs485 on + standby.
    """
    freezer.move_to("2026-01-15T13:00:00+00:00")
    await _set_lisbon(hass)  # the button reads dt_util.now()
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    select_calls = async_mock_service(hass, "select", "select_option")
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "number", "set_value")
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.battery_opt_apply_plan"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(switch_on_calls) == 1
    assert select_calls[0].data["option"] == "standby"
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "on"

    # Idempotent: the state is already commanded — a second press
    # writes nothing (the executor's write-once tracking).
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.battery_opt_apply_plan"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(switch_on_calls) == 1
    assert len(select_calls) == 1


async def test_charge_loop_wires_and_clamps_against_measured_import(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Task 15: the loop wires, feeds the CHARGE entry, clamps on spikes.

    2026-01-15 00:00 is the winter vazio charge window; flat house
    load leaves room for the full 2500 W. An AC spike (house jumps to
    2600 W) must throttle the setpoint so import stays under 4400 W.
    """
    freezer.move_to("2026-01-15T08:00:00+00:00")  # 00:00 Pacific (hass tz)
    hass.states.async_set("sensor.grid_power", "1040")
    hass.states.async_set("sensor.marstek_battery_power", "0")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **VALID_INPUT,
            "grid_power_sensor": "sensor.grid_power",
            "battery_power_sensor": "sensor.marstek_battery_power",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    loop = entry.runtime_data.charge_loop
    assert loop is not None

    async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "switch", "turn_on")
    number_calls = async_mock_service(hass, "number", "set_value")
    # Executor tick at local 00:00 (Pacific): the charge window.
    await entry.runtime_data.executor.tick(datetime(2026, 1, 15, 0, 0))
    await hass.async_block_till_done()
    entry_writes = [c.data for c in number_calls if "power" in c.data["entity_id"]]
    assert entry_writes[-1]["value"] == 2500.0  # loop-fed entry, not 2000

    # AC compressor: house load 1040 -> 2600, import spikes to 5100.
    # Battery sensor is HA-convention (positive = discharging), so a
    # 2500 W charge reads -2500; the setup wiring negates it.
    hass.states.async_set("sensor.grid_power", "5100")
    hass.states.async_set("sensor.marstek_battery_power", "-2500")
    # The CHARGE entry stamped the loop with real time.monotonic();
    # step the injected clock past the rate-limit window from there.
    await loop.on_update(now=time.monotonic() + 10.0)
    await hass.async_block_till_done()
    spike_write = number_calls[-1].data
    assert spike_write["value"] == 1800.0  # 4400 - 2600, import capped
    assert loop.fallback is False


async def test_dry_run_off_actuates_the_coordinator_greedy(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Task 12: dry_run False adopts the coordinator's validated greedy.

    With core OMIE stubbed the coordinator publishes `executor_plan`;
    the executor tick adopts it and the plan sensor reports the source.
    """
    freezer.move_to("2026-01-15T20:00:00+00:00")  # 12:00 Pacific (hass tz)
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={**VALID_INPUT, "dry_run": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert runtime.executor is not None
    assert runtime.executor.dynamic_enabled is True
    assert runtime.coordinator.executor_plan is not None

    async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "number", "set_value")
    await runtime.executor.tick(datetime(2026, 1, 15, 12, 0))
    await hass.async_block_till_done()
    assert runtime.executor.plan_source == "greedy"
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state is not None
    assert plan_state.attributes["executor_plan_source"] == "greedy"


async def test_dry_run_off_seeds_days_at_the_floor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Task 12 follow-up (owner 2026-08-13): dynamic days start at the floor.

    A greedy day ends at the reserve floor by construction (stored
    energy is unvalued), so under dynamic actuation the next day must
    NOT be seeded from the static chain's full end — instead it starts
    at the floor and the solve buys the night's cheap quarters before
    the morning ponta.
    """
    freezer.move_to("2026-07-15T08:00:00+00:00")  # summer Wednesday
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={**VALID_INPUT, "dry_run": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    soc_forecast = hass.states.get("sensor.battery_opt_soc_forecast")
    assert soc_forecast.attributes["greedy_trajectory_pct"][0] == pytest.approx(27.0)
    # The floor-seeded greedy charges BEFORE the morning ponta instead
    # of discharging energy it does not have.
    plan_state = hass.states.get("sensor.battery_opt_plan")
    early_charge = [
        s
        for s in plan_state.attributes["schedule"]
        if s["direction"] == "charge"
        and s["start"].startswith("2026-07-15")
        and datetime.fromisoformat(s["start"]).hour < 9
    ]
    assert early_charge


async def test_dry_run_defaults_on_and_keeps_static_actuation(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the option set, the executor stays on the static plan."""
    freezer.move_to("2026-01-15T20:00:00+00:00")
    _register_core_omie_service(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert runtime.executor is not None
    assert runtime.executor.dynamic_enabled is False
    # The greedy is still computed and published (the advisory dry-run)
    assert runtime.coordinator.executor_plan is not None

    async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "number", "set_value")
    await runtime.executor.tick(datetime(2026, 1, 15, 12, 0))
    await hass.async_block_till_done()
    assert runtime.executor.plan_source == "static"


async def test_actuation_switches_gate_writes_without_stopping_loops(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    The override switches skip driver writes; everything keeps running.

    Executor switch off → a tick issues no service calls but still
    updates the plan sensor; on again → the next tick replays the full
    transition (post-manual state is unknown by design).
    """
    freezer.move_to("2026-01-15T13:00:00+00:00")
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    executor_switch = hass.states.get("switch.battery_opt_executor_actuation")
    assert executor_switch is not None
    assert executor_switch.state == "on"  # default: actuation live
    # No charge-loop sensors configured -> no charge-loop switch.
    assert hass.states.get("switch.battery_opt_charge_loop_actuation") is None

    # Only select/number are mocked: the real switch domain must keep
    # serving our own toggle entities. The driver's rs485 write goes
    # to a nonexistent marstek switch — a logged warning, nothing more.
    select_calls = async_mock_service(hass, "select", "select_option")
    async_mock_service(hass, "number", "set_value")

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.battery_opt_executor_actuation"},
        blocking=True,
    )
    assert entry.runtime_data.executor.actuation_enabled is False
    await entry.runtime_data.executor.tick(datetime(2026, 1, 15, 13, 0))
    await hass.async_block_till_done()
    assert select_calls == []  # loop ran, actuation skipped
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.state == "hold"  # decision still published
    assert "actuation disabled" in plan_state.attributes["executor_status"]

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.battery_opt_executor_actuation"},
        blocking=True,
    )
    await entry.runtime_data.executor.tick(datetime(2026, 1, 15, 13, 15))
    await hass.async_block_till_done()
    assert len(select_calls) == 1  # full transition replayed
    assert select_calls[0].data["option"] == "standby"


async def test_entities_exist_and_health_follows_the_executor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Task 9: plan/savings/healthy entities; healthy mirrors ticks."""
    # Frozen so the executor's plan_day matches "today" — the SoC
    # forecast sensor only serves the executor trajectory for today.
    freezer.move_to("2026-01-15T13:00:00+00:00")
    entry = MockConfigEntry(domain=DOMAIN, data=VALID_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.battery_opt_plan").state == "unknown"
    assert hass.states.get("sensor.battery_opt_forecast_savings") is not None
    healthy = hass.states.get("binary_sensor.battery_opt_healthy")
    assert healthy.state == "off"  # no tick yet
    assert healthy.attributes["status"] == "no tick yet"

    # A quarter-hour tick (winter cheias, 13:00): HOLD, healthy on.
    # ADR-0006 from an unknown state: engage external control (rs485
    # switch on), then force standby — all service calls (ADR-0004).
    select_calls = async_mock_service(hass, "select", "select_option")
    switch_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "number", "set_value")
    await entry.runtime_data.executor.tick(datetime(2026, 1, 15, 13, 0))
    await hass.async_block_till_done()
    assert len(switch_on_calls) == 1
    assert len(select_calls) == 1
    assert select_calls[0].data["option"] == "standby"
    assert hass.states.get("binary_sensor.battery_opt_healthy").state == "on"
    plan_state = hass.states.get("sensor.battery_opt_plan")
    assert plan_state.state == "hold"
    assert plan_state.attributes["mode"] == "active"
    assert plan_state.attributes["executor_plan_date"] == "2026-01-15"

    # SoC forecast follows the ACTUATED plan once the executor ticked.
    soc_forecast = hass.states.get("sensor.battery_opt_soc_forecast")
    assert soc_forecast.attributes["source"] == "executor"
    assert len(soc_forecast.attributes["trajectory_kwh"]) == 97
    assert 27.0 <= float(soc_forecast.state) <= 100.0
