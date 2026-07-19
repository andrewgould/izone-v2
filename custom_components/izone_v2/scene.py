"""Scene entities exposing iZone favourites.

A "favourite" in the iZone app is a saved AC mode/fan/setpoint + per-zone
configuration that can be applied on demand - i.e. exactly what Home
Assistant calls a scene.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import FAVOURITE_COUNT, IZoneError, clean_string
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity


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
        try:
            await self.coordinator.api.async_execute_favourite(self._index)
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
