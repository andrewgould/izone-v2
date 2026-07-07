"""Base entities for the iZone V2 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IZoneError, clean_string
from .const import DOMAIN, MANUFACTURER
from .coordinator import IZoneCoordinator


def zone_display_name(zone: dict[str, Any]) -> str:
    """Human-readable zone name, falling back to the zone number."""
    return clean_string(zone.get("Name")) or f"Zone {int(zone.get('Index', 0)) + 1}"


class IZoneEntity(CoordinatorEntity[IZoneCoordinator]):
    """An entity belonging to the bridge/AC-unit device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        uid = coordinator.data.uid
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            manufacturer=MANUFACTURER,
            model="iZone bridge (V2 local API)",
            name=f"iZone {uid}",
        )

    @property
    def system(self) -> dict[str, Any]:
        """The current SystemV2 datagram."""
        return self.coordinator.data.system

    async def _command(self, payload: dict[str, Any]) -> None:
        """Send a command and schedule a (debounced) state refresh."""
        try:
            await self.coordinator.api.async_command(payload)
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class IZoneZoneEntity(IZoneEntity):
    """An entity belonging to a per-zone device.

    Each zone gets its own Home Assistant device named after the zone, with
    the zone name as the suggested area, so entities land in the right
    room by default and can be re-assigned per zone.
    """

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = int(index)
        uid = coordinator.data.uid
        name = zone_display_name(coordinator.data.zones[self._index])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{uid}_zone{self._index}")},
            via_device=(DOMAIN, uid),
            manufacturer=MANUFACTURER,
            model="iZone zone",
            name=name,
            suggested_area=name,
        )

    @property
    def zone(self) -> dict[str, Any]:
        """The current ZonesV2 datagram for this zone."""
        return self.coordinator.data.zones[self._index]
