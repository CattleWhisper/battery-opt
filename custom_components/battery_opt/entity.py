"""
Shared device identity for battery_opt entities.

All entities attach to one service-type device (like core OMIE's),
giving them a single management page in the UI.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def device_info_for(entry_id: str) -> DeviceInfo:
    """Return the shared service device for a config entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Battery Opt",
        manufacturer="battery_opt",
        model="TAR arbitrage planner",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://github.com/CattleWhisper/battery-opt",
    )
