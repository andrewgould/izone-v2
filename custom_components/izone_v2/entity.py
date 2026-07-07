"""Base entity for the iZone V2 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IZoneError
from .const import DOMAIN, MANUFACTURER
from .coordinator import IZoneCoordinator


def clean_string(value: Any) -> str:
    """Strip firmware padding (NUL / 0xFF bytes) from a wire string."""
    if value is None:
        return ""
    # Names are fixed-size buffers; content ends at the first NUL and any
    # remaining bytes are 0xFF padding (decoded as 'ÿ' via latin-1).
    return str(value).split("\x00", 1)[0].strip("\xff \t\r\n")


def temp_from_wire(value: Any) -> float | None:
    """Convert a wire temperature (Celsius x100) to degrees, if plausible."""
    try:
        temp = int(value) / 100
    except (TypeError, ValueError):
        return None
    # Zones without a sensor report 0 / garbage.
    if temp < -20 or temp > 60 or temp == 0:
        return None
    return temp


class IZoneEntity(CoordinatorEntity[IZoneCoordinator]):
    """Common behaviour: device info, command dispatch, refresh-after-write."""

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
