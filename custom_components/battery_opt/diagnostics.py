"""
Diagnostics for battery_opt (plan Task 10 comfort, decision 10).

Nothing secret lives in the config entry or the coordinator's data
snapshot — entity ids, tariff/battery parameters and computed plans
are not sensitive, so no redaction is needed here (contrast with
integrations that hold API keys or credentials).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import BatteryOptConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 - HA-required signature
    entry: BatteryOptConfigEntry,
) -> dict[str, Any]:
    """Return the entry's data/options and the coordinator's last snapshot."""
    coordinator = entry.runtime_data.coordinator
    return {
        "entry_data": dict(entry.data),
        "entry_options": dict(entry.options),
        # ADR-0009: each battery of the fleet is a subentry.
        "subentries": {
            subentry.subentry_id: {
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "data": dict(subentry.data),
            }
            for subentry in entry.subentries.values()
        },
        "coordinator_data": coordinator.data,
    }
