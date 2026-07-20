"""Diagnostics support for the iZone V2 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .api import FAVOURITE_COUNT, IZoneError
from .coordinator import IZoneConfigEntry

TO_REDACT = {"AirStreamDeviceUId", "Pass", "LockCode"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IZoneConfigEntry
) -> dict[str, Any]:
    """Return the raw bridge state for issue reports."""
    coordinator = entry.runtime_data
    favourites = []
    for index in range(FAVOURITE_COUNT):
        try:
            favourites.append(await coordinator.api.async_get_favourite(index))
        except IZoneError as err:
            favourites.append({"index": index, "error": str(err)})
    return {
        "entry": async_redact_data(dict(entry.data), {"uid"}),
        "system": async_redact_data(coordinator.data.system, TO_REDACT),
        "zones": coordinator.data.zones,
        "favourites": favourites,
        "last_update_success": coordinator.last_update_success,
        "recent_command_failures": coordinator.recent_command_failures,
        "bridge_overloaded": coordinator.bridge_overloaded,
    }
