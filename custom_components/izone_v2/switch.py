"""Switch entities for open/close (damper-only) zones."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ZoneMode, ZoneType
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, clean_string


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose open/close zones as switches (open = on)."""
    coordinator = entry.runtime_data
    async_add_entities(
        IZoneZoneSwitch(coordinator, zone["Index"])
        for zone in coordinator.data.zones
        if zone.get("ZoneType") in (ZoneType.OPEN_CLOSE, ZoneType.CONSTANT)
    )


class IZoneZoneSwitch(IZoneEntity, SwitchEntity):
    """An open/close or constant zone damper."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = int(index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}"

    @property
    def zone(self) -> dict[str, Any]:
        return self.coordinator.data.zones[self._index]

    @property
    def name(self) -> str:
        return clean_string(self.zone.get("Name")) or f"Zone {self._index + 1}"

    @property
    def is_on(self) -> bool:
        return int(self.zone.get("Mode", 0)) != ZoneMode.CLOSE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"damper_position": self.zone.get("DmpPos")}
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
