"""Data update coordinator for the iZone V2 integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IZoneApi, IZoneError
from .const import DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

type IZoneConfigEntry = ConfigEntry[IZoneCoordinator]


@dataclass
class IZoneData:
    """State snapshot of an iZone system."""

    uid: str
    system: dict[str, Any]  # SystemV2 datagram
    zones: list[dict[str, Any]]  # ZonesV2 datagrams, index == zone index


class IZoneCoordinator(DataUpdateCoordinator[IZoneData]):
    """Polls the bridge over the V2 local API."""

    config_entry: IZoneConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: IZoneConfigEntry, api: IZoneApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> IZoneData:
        try:
            response = await self.api.async_get_system()
            system = response["SystemV2"]
            # Zones must be fetched one at a time (Type 2, No = index) and
            # sequentially - the bridge can't handle concurrent requests.
            zones = [
                await self.api.async_get_zone(index)
                for index in range(int(system.get("NoOfZones", 0)))
            ]
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        return IZoneData(
            uid=str(response.get("AirStreamDeviceUId", "")), system=system, zones=zones
        )
