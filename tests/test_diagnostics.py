"""
Smoke test for diagnostics.py (plan Task 10 comfort, decision 10).

Confirms the config entry's data/options and the coordinator's last
data snapshot come back without error and without needing redaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_opt.const import (
    CONF_CAPACITY_KWH,
    CONF_PLAN_WEAR,
    CONF_RESERVE_FLOOR_PCT,
    CONF_WEAR_COST,
    DOMAIN,
)
from custom_components.battery_opt.diagnostics import (
    async_get_config_entry_diagnostics,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PARAMETERS = {
    CONF_CAPACITY_KWH: 5.0,
    CONF_RESERVE_FLOOR_PCT: 27.0,
    CONF_WEAR_COST: 0.020,
    CONF_PLAN_WEAR: 0.0467,
}


async def test_diagnostics_returns_entry_and_coordinator_snapshot(
    hass: HomeAssistant,
) -> None:
    """Diagnostics carry entry data/options plus the coordinator's data dict."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(PARAMETERS))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_data"] == dict(PARAMETERS)
    assert diagnostics["entry_options"] == {}
    assert diagnostics["coordinator_data"] is not None
    assert "prices_ok" in diagnostics["coordinator_data"]
    assert "plan_charge_w" in diagnostics["coordinator_data"]
