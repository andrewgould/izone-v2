"""Data update coordinator for the iZone V2 integration."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IZoneApi, IZoneError, ZoneMode
from .const import (
    COMMAND_FAILURE_WINDOW,
    DOMAIN,
    OVERLOAD_THRESHOLD,
    POLL_INTERVAL,
    SCENE_DEFER_EXPIRY,
)

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
        # Monotonic timestamps of recent failed commands, for the
        # bridge-overload signal. Bounded so a runaway can't grow unbounded.
        self._command_failures: deque[float] = deque(maxlen=64)
        api.on_command_result = self._note_command_result
        # Zone targets from a scene that couldn't be applied because the
        # zone's sensor was faulted, keyed by zone index:
        # index -> (expiry_monotonic, mode, setpoint_wire_or_None). Re-applied
        # on a later poll once the sensor recovers (see _reapply_deferred).
        self._deferred_zones: dict[int, tuple[float, int, int | None]] = {}

    @callback
    def _note_command_result(self, ok: bool) -> None:
        """Record a command outcome and push the health state to listeners."""
        now = self.hass.loop.time()
        if not ok:
            self._command_failures.append(now)
        self._prune_failures(now)
        self.async_update_listeners()

    def _prune_failures(self, now: float) -> None:
        while self._command_failures and (
            now - self._command_failures[0] > COMMAND_FAILURE_WINDOW
        ):
            self._command_failures.popleft()

    @property
    def recent_command_failures(self) -> int:
        """Number of failed commands within the recent window."""
        self._prune_failures(self.hass.loop.time())
        return len(self._command_failures)

    @property
    def bridge_overloaded(self) -> bool:
        """True when commands are failing often enough to warrant action."""
        return self.recent_command_failures >= OVERLOAD_THRESHOLD

    # -- deferred scene zones ---------------------------------------------

    def defer_zone_target(self, index: int, mode: int, setpoint: int | None) -> None:
        """Remember a scene's target for a zone whose sensor is faulted.

        Re-applied automatically by a later poll once the sensor recovers, or
        dropped after SCENE_DEFER_EXPIRY if it never does.
        """
        expiry = self.hass.loop.time() + SCENE_DEFER_EXPIRY
        self._deferred_zones[index] = (expiry, mode, setpoint)

    def clear_deferred_zones(self) -> None:
        """Forget all pending deferrals (a newer scene supersedes them)."""
        self._deferred_zones.clear()

    async def _reapply_deferred(self, zones: list[dict[str, Any]]) -> None:
        """Re-apply deferred zone targets whose sensors have recovered.

        Runs inside the poll (so it's serialised with reads); a send that
        fails is left pending for the next poll rather than failing the poll.
        """
        if not self._deferred_zones:
            return
        now = self.hass.loop.time()
        by_index = {int(z.get("Index", -1)): z for z in zones}
        for index in list(self._deferred_zones):
            expiry, mode, setpoint = self._deferred_zones[index]
            if now > expiry:
                _LOGGER.info(
                    "Dropping deferred scene target for zone %d - its sensor did "
                    "not recover within the retry window",
                    index,
                )
                del self._deferred_zones[index]
                continue
            zone = by_index.get(index)
            if zone is None or zone.get("SensorFault"):
                continue  # still faulted (or gone), keep waiting
            try:
                await self.api.async_set_zone_mode(index, ZoneMode(mode))
                if setpoint is not None:
                    await self.api.async_set_zone_setpoint(index, setpoint / 100)
            except IZoneError as err:
                _LOGGER.debug(
                    "Deferred re-apply for zone %d failed, will retry: %s", index, err
                )
                continue
            _LOGGER.info(
                "Zone %d sensor recovered - applied its deferred scene target", index
            )
            del self._deferred_zones[index]

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
            await self._reapply_deferred(zones)
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        return IZoneData(
            uid=str(response.get("AirStreamDeviceUId", "")), system=system, zones=zones
        )
