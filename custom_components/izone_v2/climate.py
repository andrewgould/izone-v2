"""Climate entities: the AC unit and any temperature-controlled zones."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_TOP,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SysFan, SysMode, ZoneMode, ZoneType, temp_from_wire
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity

_LOGGER = logging.getLogger(__name__)

# SysMode_e <-> Home Assistant HVAC modes
MODE_TO_HVAC: dict[int, HVACMode] = {
    SysMode.COOL: HVACMode.COOL,
    SysMode.HEAT: HVACMode.HEAT,
    SysMode.VENT: HVACMode.FAN_ONLY,
    SysMode.DRY: HVACMode.DRY,
    SysMode.AUTO: HVACMode.HEAT_COOL,
    # Coolbreeze-only modes; best-effort representation, not settable from HA.
    SysMode.EXHAUST: HVACMode.FAN_ONLY,
    SysMode.PUMP_ONLY: HVACMode.DRY,
}
HVAC_TO_MODE: dict[HVACMode, SysMode] = {
    HVACMode.COOL: SysMode.COOL,
    HVACMode.HEAT: SysMode.HEAT,
    HVACMode.FAN_ONLY: SysMode.VENT,
    HVACMode.DRY: SysMode.DRY,
    HVACMode.HEAT_COOL: SysMode.AUTO,
}

# SysFan_e <-> Home Assistant fan modes
FAN_TO_HA: dict[int, str] = {
    SysFan.LOW: FAN_LOW,
    SysFan.MED: FAN_MEDIUM,
    SysFan.HIGH: FAN_HIGH,
    SysFan.AUTO: FAN_AUTO,
    SysFan.TOP: FAN_TOP,
    SysFan.QUIET: "quiet",
    SysFan.TURBO: "turbo",
}
HA_TO_FAN: dict[str, SysFan] = {v: k for k, v in FAN_TO_HA.items()}

# ZoneMode_e <-> HVAC modes for zone entities
ZONE_MODE_TO_HVAC: dict[int, HVACMode] = {
    ZoneMode.OPEN: HVACMode.FAN_ONLY,
    ZoneMode.CLOSE: HVACMode.OFF,
    ZoneMode.AUTO: HVACMode.HEAT_COOL,
    ZoneMode.OVERRIDE: HVACMode.HEAT_COOL,
    ZoneMode.CONSTANT: HVACMode.FAN_ONLY,
}

# Absolute limits from the spec (used when EcoLock is off)
TEMP_MIN = 15.0
TEMP_MAX = 30.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AC unit and climate-controlled zone entities."""
    coordinator = entry.runtime_data
    entities: list[ClimateEntity] = [IZoneAcClimate(coordinator)]
    entities.extend(
        IZoneZoneClimate(coordinator, zone["Index"])
        for zone in coordinator.data.zones
        if zone.get("ZoneType") == ZoneType.AUTO
    )
    async_add_entities(entities)


class IZoneClimateMixin(ClimateEntity):
    """Shared temperature-limit handling."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = PRECISION_HALVES

    @property
    def min_temp(self) -> float:
        if self.system.get("EcoLock"):
            return (temp_from_wire(self.system.get("EcoMin")) or TEMP_MIN)
        return TEMP_MIN

    @property
    def max_temp(self) -> float:
        if self.system.get("EcoLock"):
            return (temp_from_wire(self.system.get("EcoMax")) or TEMP_MAX)
        return TEMP_MAX

    def _validated_setpoint(self, kwargs: dict[str, Any]) -> float:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            raise ServiceValidationError("No target temperature provided")
        temp = round(float(temp) * 2) / 2  # the system works in 0.5C steps
        return min(max(temp, self.min_temp), self.max_temp)

    async def _apply_hvac_mode(self, kwargs: dict[str, Any]) -> None:
        """Honour an hvac_mode bundled into a set_temperature call.

        Home Assistant allows ``climate.set_temperature`` to carry an
        ``hvac_mode``; standard climate entities set the mode before the
        setpoint. We do the same, but reject a mode the entity doesn't support
        with a clear error instead of a cryptic failure - a zone has no
        'heat'/'cool' of its own (that's a whole-unit mode), so pointing a
        blanket "heat everything to 22" at both the unit and its zones used to
        fail obscurely.
        """
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is None:
            return
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                f"{self.entity_id} does not support the '{hvac_mode}' mode; "
                f"supported modes: {', '.join(self.hvac_modes)}"
            )
        await self.async_set_hvac_mode(hvac_mode)


class IZoneAcClimate(IZoneEntity, IZoneClimateMixin):
    """The AC unit itself."""

    _attr_name = None  # take the device name
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.HEAT_COOL,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]
    # Per the developer docs: 1=Low, 2=Med, 3=High, 4=Auto, 5=Top
    _attr_fan_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_AUTO, FAN_TOP]

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.data.uid

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.system.get("SysOn"):
            return HVACMode.OFF
        return MODE_TO_HVAC.get(int(self.system.get("SysMode", 0)), HVACMode.HEAT_COOL)

    @property
    def fan_mode(self) -> str | None:
        return FAN_TO_HA.get(int(self.system.get("SysFan", 0)))

    @property
    def current_temperature(self) -> float | None:
        return temp_from_wire(self.system.get("Temp"))

    @property
    def target_temperature(self) -> float | None:
        return temp_from_wire(self.system.get("Setpoint"))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._command({"SysOn": 0})
            return
        if not self.system.get("SysOn"):
            await self._command({"SysOn": 1})
        await self._command({"SysMode": int(HVAC_TO_MODE[hvac_mode])})

    async def async_turn_on(self) -> None:
        await self._command({"SysOn": 1})

    async def async_turn_off(self) -> None:
        await self._command({"SysOn": 0})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._command({"SysFan": int(HA_TO_FAN[fan_mode])})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        await self._apply_hvac_mode(kwargs)
        temp = self._validated_setpoint(kwargs)
        await self._command({"SysSetpoint": round(temp * 100)})


class IZoneZoneClimate(IZoneZoneEntity, IZoneClimateMixin):
    """A temperature-controlled (ZoneType Auto) zone."""

    _attr_name = None  # take the zone-device name
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # OFF = damper closed, FAN_ONLY = damper open, HEAT_COOL = climate control
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.HEAT_COOL]

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.data.uid}_zone{self._index}"

    @property
    def hvac_mode(self) -> HVACMode:
        return ZONE_MODE_TO_HVAC.get(int(self.zone.get("Mode", 0)), HVACMode.OFF)

    @property
    def current_temperature(self) -> float | None:
        if self.zone.get("SensorFault"):
            return None
        return temp_from_wire(self.zone.get("Temp"))

    @property
    def target_temperature(self) -> float | None:
        return temp_from_wire(self.zone.get("Setpoint"))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = {
            HVACMode.OFF: ZoneMode.CLOSE,
            HVACMode.FAN_ONLY: ZoneMode.OPEN,
            HVACMode.HEAT_COOL: ZoneMode.AUTO,
        }.get(hvac_mode)
        if mode is None:
            # heat/cool/dry are whole-unit modes; a zone only opens, closes, or
            # climate-controls (following the unit). Say so instead of KeyError.
            raise ServiceValidationError(
                f"{self.entity_id} is a zone and has no '{hvac_mode}' mode - set "
                "the AC unit's mode instead; a zone can only be off, fan-only, "
                "or climate-controlled (heat_cool)"
            )
        await self._command({"ZoneMode": {"Index": self._index, "Mode": int(mode)}})

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        await self._apply_hvac_mode(kwargs)
        temp = self._validated_setpoint(kwargs)
        await self._command(
            {"ZoneSetpoint": {"Index": self._index, "Setpoint": round(temp * 100)}}
        )
