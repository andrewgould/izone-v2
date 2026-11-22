"""Button entities for the iZone V2 integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import IZoneError
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the filter reset button."""
    async_add_entities([IZoneFilterResetButton(entry.runtime_data)])


class IZoneFilterResetButton(IZoneEntity, ButtonEntity):
    """Clears the controller's filter warning, as after cleaning the filter.

    It restarts the reminder timer, the same as resetting it on the wall
    controller or in the iZone app.
    """

    _attr_name = "Reset filter warning"
    _attr_icon = "mdi:air-filter"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_reset_filter_warning"

    async def async_press(self) -> None:
        try:
            cleared = await self.coordinator.api.async_reset_filter_warning()
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
        if not cleared:
            raise HomeAssistantError(
                "The iZone controller still reports the filter warning after the "
                "reset; try again, or reset it on the wall controller"
            )
