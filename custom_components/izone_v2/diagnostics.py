"""Diagnostics support for the iZone V2 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import IZoneConfigEntry

TO_REDACT = {"AirStreamDeviceUId", "Pass", "LockCode"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IZoneConfigEntry
) -> dict[str, Any]:
    """Return the raw bridge state for issue reports."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), {"uid"}),
        "system": async_redact_data(coordinator.data.system, TO_REDACT),
        "zones": coordinator.data.zones,
        "last_update_success": coordinator.last_update_success,
    }
