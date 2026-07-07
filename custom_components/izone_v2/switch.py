"""Switch entities: open/close zone dampers and the iSave function."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ZoneMode, ZoneType
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose open/close zones as switches (open = on), plus iSave."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        IZoneZoneSwitch(coordinator, zone["Index"])
        for zone in coordinator.data.zones
        if zone.get("ZoneType") in (ZoneType.OPEN_CLOSE, ZoneType.CONSTANT)
    ]
    if coordinator.data.system.get("iSaveEnable"):
        entities.append(IZoneISaveSwitch(coordinator))
    async_add_entities(entities)


class IZoneZoneSwitch(IZoneZoneEntity, SwitchEntity):
    """An open/close or constant zone damper."""

    _attr_name = None  # take the zone-device name
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}"

    @property
    def is_on(self) -> bool:
        return int(self.zone.get("Mode", 0)) != ZoneMode.CLOSE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self.zone.get("ZoneType") == ZoneType.CONSTANT:
            attrs["constant_zone"] = True
            attrs["constant_active"] = bool(self.zone.get("ConstA"))
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._command(
            {"ZoneMode": {"Index": self._index, "Mode": int(ZoneMode.OPEN)}}
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._command(
            {"ZoneMode": {"Index": self._index, "Mode": int(ZoneMode.CLOSE)}}
        )


class IZoneISaveSwitch(IZoneEntity, SwitchEntity):
    """The iSave economy function, when the system supports it."""

    _attr_name = "iSave"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:leaf"

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_isave"

    @property
    def is_on(self) -> bool:
        return bool(self.system.get("iSaveOn"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._command({"iSaveOn": 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._command({"iSaveOn": 0})
