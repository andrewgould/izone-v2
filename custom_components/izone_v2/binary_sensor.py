"""Binary sensor entities for the iZone V2 integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ROOM_SENSOR_NONE, clean_string
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up warning/fault binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        IZoneFilterWarningSensor(coordinator),
        IZoneBridgeOverloadedSensor(coordinator),
    ]
    for zone in coordinator.data.zones:
        index = zone["Index"]
        entities.append(IZoneDamperFaultSensor(coordinator, index))
        if zone.get("SensType", ROOM_SENSOR_NONE) != ROOM_SENSOR_NONE:
            entities.append(IZoneSensorFaultSensor(coordinator, index))
    async_add_entities(entities)


class IZoneFilterWarningSensor(IZoneEntity, BinarySensorEntity):
    """On when the system reports the filter needs attention."""

    _attr_name = "Filter warning"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_filter_warning"

    @property
    def is_on(self) -> bool:
        # Documented values for "Warnings": "none", "filter"
        return clean_string(self.system.get("Warnings")).lower() == "filter"


class IZoneBridgeOverloadedSensor(IZoneEntity, BinarySensorEntity):
    """On when commands are repeatedly failing (bridge wedged/overloaded).

    Intended as an automation trigger: e.g. power-cycle the iZone hub /
    controller when this stays on. It reflects command failures (busy
    exhaustion or connection errors), not poll blips.
    """

    _attr_name = "Bridge overloaded"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_bridge_overloaded"

    @property
    def is_on(self) -> bool:
        return self.coordinator.bridge_overloaded

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"recent_command_failures": self.coordinator.recent_command_failures}


class IZoneDamperFaultSensor(IZoneZoneEntity, BinarySensorEntity):
    """On when the zone damper motor reports a fault."""

    _attr_name = "Damper fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_damper_fault"

    @property
    def is_on(self) -> bool:
        return bool(self.zone.get("DmpFlt"))


class IZoneSensorFaultSensor(IZoneZoneEntity, BinarySensorEntity):
    """On when the zone temperature sensor reports a fault."""

    _attr_name = "Sensor fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_sensor_fault"

    @property
    def is_on(self) -> bool:
        return bool(self.zone.get("SensorFault"))
