"""Sensor entities for the iZone V2 integration."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ROOM_SENSOR_NONE, ROOM_SENSOR_WIRELESS, clean_string, temp_from_wire
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity

# BatteryLevel_e
BATTERY_LEVELS = {0: "full", 1: "half", 2: "empty"}

# RfSignalLevel_e
RF_SIGNAL_LEVELS = {0: "full", 1: "half", 2: "quarter", 3: "none"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up bridge and per-zone sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        IZoneSupplyTempSensor(coordinator),
        IZoneReturnTempSensor(coordinator),
        IZoneAcErrorSensor(coordinator),
        IZoneSleepRemainingSensor(coordinator),
    ]
    for zone in coordinator.data.zones:
        index = zone["Index"]
        entities.append(IZoneDamperPositionSensor(coordinator, index))
        if zone.get("SensType", ROOM_SENSOR_NONE) != ROOM_SENSOR_NONE:
            entities.append(IZoneZoneTempSensor(coordinator, index))
        if zone.get("SensType") == ROOM_SENSOR_WIRELESS and "BattVolt" in zone:
            entities.append(IZoneZoneBatterySensor(coordinator, index))
        if zone.get("SensType") == ROOM_SENSOR_WIRELESS and "RfSignal" in zone:
            entities.append(IZoneZoneRfSignalSensor(coordinator, index))
    async_add_entities(entities)


class IZoneSupplyTempSensor(IZoneEntity, SensorEntity):
    """Supply (in-duct) air temperature."""

    _attr_name = "Supply air temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_supply_temp"

    @property
    def native_value(self) -> float | None:
        return temp_from_wire(self.system.get("Supply"))


class IZoneReturnTempSensor(IZoneEntity, SensorEntity):
    """Return air temperature."""

    _attr_name = "Return air temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_return_temp"

    @property
    def native_value(self) -> float | None:
        return temp_from_wire(self.system.get("Temp"))


class IZoneAcErrorSensor(IZoneEntity, SensorEntity):
    """AC unit error code (' OK' means no error)."""

    _attr_name = "AC error code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_ac_error"

    @property
    def native_value(self) -> str:
        return clean_string(self.system.get("ACError")) or "OK"


class IZoneSleepRemainingSensor(IZoneEntity, SensorEntity):
    """Minutes left on the sleep timer."""

    _attr_name = "Sleep timer remaining"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_sleep_remaining"

    @property
    def native_value(self) -> int:
        try:
            return int(self.system.get("SleepTimerM", 0))
        except (TypeError, ValueError):
            return 0


class IZoneZoneTempSensor(IZoneZoneEntity, SensorEntity):
    """Zone temperature (for zones with a sensor)."""

    _attr_name = None  # device-class name: "<Zone> Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_temp"

    @property
    def native_value(self) -> float | None:
        if self.zone.get("SensorFault"):
            return None
        return temp_from_wire(self.zone.get("Temp"))


class IZoneDamperPositionSensor(IZoneZoneEntity, SensorEntity):
    """Current damper position of a zone."""

    _attr_name = "Damper position"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:valve"

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_damper"

    @property
    def native_value(self) -> int | None:
        try:
            return int(self.zone["DmpPos"])
        except (KeyError, TypeError, ValueError):
            return None


class IZoneZoneBatterySensor(IZoneZoneEntity, SensorEntity):
    """Battery level of a wireless zone sensor (full/half/empty)."""

    _attr_name = "Sensor battery"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = list(BATTERY_LEVELS.values())
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_battery"

    @property
    def native_value(self) -> str | None:
        return BATTERY_LEVELS.get(self.zone.get("BattVolt"))


class IZoneZoneRfSignalSensor(IZoneZoneEntity, SensorEntity):
    """RF signal strength of a wireless zone sensor.

    Tends to degrade before a sensor drops out entirely (goes "unknown" on
    its temperature sensor) - watching this can give earlier warning than
    waiting for the fault binary sensor to trip.
    """

    _attr_name = "Sensor signal"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = list(RF_SIGNAL_LEVELS.values())
    _attr_icon = "mdi:signal"

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_rf_signal"

    @property
    def native_value(self) -> str | None:
        return RF_SIGNAL_LEVELS.get(self.zone.get("RfSignal"))
