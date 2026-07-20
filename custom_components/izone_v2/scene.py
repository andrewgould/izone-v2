"""Scene entities exposing iZone favourites.

A "favourite" in the iZone app is a saved AC mode/fan/setpoint + per-zone
configuration that can be applied on demand - i.e. exactly what Home
Assistant calls a scene.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    FAVOURITE_COUNT,
    IZoneError,
    clean_string,
    favourite_target_reached,
)
from .const import SCENE_VERIFY_DELAY, SCENE_VERIFY_RETRIES
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover configured favourites and expose them as scenes.

    Favourites are read once at setup rather than on every coordinator
    poll - they rarely change and the bridge only handles one request at
    a time, so there's no reason to add 9 more requests to every 30s
    refresh. Reload the integration after adding/renaming a favourite in
    the iZone app to pick up the change.
    """
    coordinator = entry.runtime_data
    entities: list[Scene] = []
    for index in range(FAVOURITE_COUNT):
        try:
            favourite = await coordinator.api.async_get_favourite(index)
        except IZoneError:
            continue
        name = clean_string(favourite.get("Name"))
        if not name:
            continue  # unused favourite slot
        entities.append(IZoneFavouriteScene(coordinator, index, name))
    async_add_entities(entities)


class IZoneFavouriteScene(IZoneEntity, Scene):
    """A saved iZone favourite."""

    _attr_icon = "mdi:star-four-points-outline"

    def __init__(self, coordinator: IZoneCoordinator, index: int, name: str) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.data.uid}_favourite{index}"

    @property
    def available(self) -> bool:
        # A scene is a fire-and-forget action, not a state readout. Keep it
        # triggerable even when the last poll blipped (the bridge drops the
        # odd request); the command path retries transient failures and
        # surfaces a clear error if the bridge is genuinely unreachable.
        # Without this, a single timed-out poll would flip the scene to
        # "unavailable" and silently drop scene.turn_on calls.
        return True

    async def async_activate(self, **kwargs: Any) -> None:
        # Fetch the favourite's current target so we can confirm it applied.
        # (Fetched fresh in case it was edited in the iZone app since setup.)
        try:
            target = await self.coordinator.api.async_get_favourite(self._index)
        except IZoneError:
            target = None

        for attempt in range(SCENE_VERIFY_RETRIES + 1):
            try:
                await self.coordinator.api.async_execute_favourite(self._index)
            except IZoneError as err:
                raise HomeAssistantError(str(err)) from err

            if target is None:
                # Can't verify without the target; fall back to a single shot.
                break

            # Give the controller time to actuate, then read back and confirm.
            await asyncio.sleep(SCENE_VERIFY_DELAY)
            await self.coordinator.async_refresh()
            if favourite_target_reached(target, self.coordinator.data.zones):
                return

            _LOGGER.debug(
                "iZone favourite '%s' not fully applied yet (attempt %d/%d)",
                self._attr_name,
                attempt + 1,
                SCENE_VERIFY_RETRIES + 1,
            )
        else:
            _LOGGER.warning(
                "iZone favourite '%s' could not be confirmed applied after %d "
                "attempts - the controller may be busy or a zone sensor faulty",
                self._attr_name,
                SCENE_VERIFY_RETRIES + 1,
            )

        await self.coordinator.async_request_refresh()
