"""Number entities: sleep timer and per-zone airflow limits."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sleep timer and zone airflow numbers."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [IZoneSleepTimerNumber(coordinator)]
    for zone in coordinator.data.zones:
        entities.append(IZoneAirflowNumber(coordinator, zone["Index"], minimum=True))
        entities.append(IZoneAirflowNumber(coordinator, zone["Index"], minimum=False))
    async_add_entities(entities)


class IZoneSleepTimerNumber(IZoneEntity, NumberEntity):
    """The system sleep timer (minutes, 0 = off)."""

    _attr_name = "Sleep timer"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 120
    _attr_native_step = 30  # the iZone app offers 0/30/60/90/120
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.uid}_sleep_timer"

    @property
    def native_value(self) -> int:
        try:
            return int(self.system.get("SleepTimer", 0))
        except (TypeError, ValueError):
            return 0

    async def async_set_native_value(self, value: float) -> None:
        await self._command({"SysSleepTimer": int(value)})


class IZoneAirflowNumber(IZoneZoneEntity, NumberEntity):
    """Zone minimum or maximum damper opening (%)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5  # the spec allows 5% increments only
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:valve"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: IZoneCoordinator, index: int, *, minimum: bool
    ) -> None:
        super().__init__(coordinator, index)
        self._minimum = minimum
        kind = "min" if minimum else "max"
        self._attr_name = f"Airflow {kind}imum"
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}_{kind}_air"

    @property
    def native_value(self) -> int | None:
        try:
            return int(self.zone["MinAir" if self._minimum else "MaxAir"])
        except (KeyError, TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        percent = int(value)
        if self._minimum:
            await self._command(
                {"ZoneMinAir": {"Index": self._index, "MinAir": percent}}
            )
        else:
            await self._command(
                {"ZoneMaxAir": {"Index": self._index, "MaxAir": percent}}
            )
