"""Scene entities exposing iZone favourites.

A "favourite" in the iZone app is a saved AC mode/fan/setpoint + per-zone
configuration that can be applied on demand - i.e. exactly what Home
Assistant calls a scene.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    FAVOURITE_COUNT,
    IZoneError,
    SysFan,
    SysMode,
    ZoneApply,
    ZoneMode,
    ZoneType,
    clean_string,
    describe_favourite_zones,
    favourite_mismatches,
    favourite_target_reached,
    favourite_zones_with,
    plan_favourite_zones,
)
from .const import (
    FAVOURITE_EDIT_ATTEMPTS,
    FAVOURITE_WRITE_SETTLE,
    SCENE_VERIFY_DELAY,
    SCENE_VERIFY_RETRIES,
)
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity

_LOGGER = logging.getLogger(__name__)

SERVICE_UPDATE_FAVOURITE = "update_favourite"

# Service choices -> wire values. "unset" (0) means the favourite leaves the
# unit's fan or mode alone, which is how favourites made in the app store them.
FAN_CHOICES = {
    "unset": 0,
    "low": SysFan.LOW,
    "medium": SysFan.MED,
    "high": SysFan.HIGH,
    "auto": SysFan.AUTO,
    "top": SysFan.TOP,
}
MODE_CHOICES = {
    "unset": 0,
    "cool": SysMode.COOL,
    "heat": SysMode.HEAT,
    "vent": SysMode.VENT,
    "dry": SysMode.DRY,
    "auto": SysMode.AUTO,
}
ZONE_MODE_CHOICES = {
    "open": ZoneMode.OPEN,
    "close": ZoneMode.CLOSE,
    "auto": ZoneMode.AUTO,
}
FAN_NAMES = {int(v): k for k, v in FAN_CHOICES.items()}
MODE_NAMES = {int(v): k for k, v in MODE_CHOICES.items()}

UPDATE_FAVOURITE_SCHEMA = {
    vol.Optional("zone"): cv.string,
    vol.Optional("zone_mode"): vol.In(list(ZONE_MODE_CHOICES)),
    vol.Optional("temperature"): vol.All(
        vol.Coerce(float), vol.Range(min=15, max=30)
    ),
    vol.Optional("fan"): vol.In(list(FAN_CHOICES)),
    vol.Optional("mode"): vol.In(list(MODE_CHOICES)),
}


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
        entities.append(IZoneFavouriteScene(coordinator, index, name, favourite))
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_UPDATE_FAVOURITE, UPDATE_FAVOURITE_SCHEMA, "async_update_favourite"
    )


class IZoneFavouriteScene(IZoneEntity, Scene):
    """A saved iZone favourite."""

    _attr_icon = "mdi:star-four-points-outline"

    def __init__(
        self,
        coordinator: IZoneCoordinator,
        index: int,
        name: str,
        favourite: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._favourite = favourite
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.data.uid}_favourite{index}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the favourite stores, as last read from the bridge.

        Read at setup, and again whenever the scene is activated or edited
        through ``izone_v2.update_favourite``.
        """
        attrs: dict[str, Any] = {"favourite_number": self._index + 1}
        if self._favourite is None:
            return attrs
        attrs["zones"] = describe_favourite_zones(
            self._favourite, self.coordinator.data.zones
        )
        attrs["fan"] = FAN_NAMES.get(int(self._favourite.get("Fan", 0)), "unknown")
        attrs["mode"] = MODE_NAMES.get(int(self._favourite.get("Mode", 0)), "unknown")
        return attrs

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
        else:
            self._favourite = target

        # A newer scene supersedes whatever an earlier one was still applying.
        self.coordinator.replace_zone_targets({})

        # If the favourite drives a climate zone whose sensor is faulted, the
        # controller won't apply the favourite as a unit - apply it ourselves,
        # zone by zone, and leave the faulted climate zones for later.
        plan = (
            plan_favourite_zones(target, self.coordinator.data.zones)
            if target is not None
            else []
        )
        if any(action.defer for action in plan):
            await self._activate_manually(target, plan)
        else:
            await self._activate_via_controller(target, plan)

    async def async_update_favourite(
        self,
        zone: str | None = None,
        zone_mode: str | None = None,
        temperature: float | None = None,
        fan: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Edit what this favourite stores (``izone_v2.update_favourite``).

        Changes one zone (``zone`` with ``zone_mode`` and/or ``temperature``),
        the fan and/or the AC mode. The edit is written to the controller, so
        the iZone app and wall controller see it too. Nothing is applied: the
        favourite only takes effect the next time it is activated.
        """
        if zone is None and (zone_mode is not None or temperature is not None):
            raise ServiceValidationError("zone_mode and temperature need a zone")
        if zone is not None and zone_mode is None and temperature is None:
            raise ServiceValidationError("Give a zone_mode and/or temperature for the zone")
        if zone is None and fan is None and mode is None:
            raise ServiceValidationError("Nothing to change")

        api = self.coordinator.api
        try:
            current = await api.async_get_favourite(self._index)
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err

        # Each edit is a (label, send, kept) triple: kept() checks the
        # favourite as read back from the controller.
        edits: list[tuple[str, Callable[[], Awaitable[None]], Callable[[dict], bool]]] = []
        if zone is not None:
            index = self._zone_index(zone)
            stored_zones = current.get("Zones") or []
            stored = stored_zones[index] if index < len(stored_zones) else {}
            new_mode = (
                ZONE_MODE_CHOICES[zone_mode]
                if zone_mode is not None
                else ZoneMode(int(stored.get("Mode", ZoneMode.CLOSE)))
            )
            zone_data = self.coordinator.data.zones[index]
            if (
                new_mode == ZoneMode.AUTO
                and int(zone_data.get("ZoneType", 0)) != ZoneType.AUTO
            ):
                raise ServiceValidationError(
                    f"{zone} has no temperature control, so it can only be open or close"
                )
            setpoint = None if temperature is None else round(temperature * 2) * 50
            try:
                zones = favourite_zones_with(current, index, new_mode, setpoint)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            edits.append(
                (
                    f"zone {zone}",
                    lambda: api.async_set_favourite_zones(self._index, zones),
                    lambda fav: (fav.get("Zones") or [])[index : index + 1]
                    == [zones[index]],
                )
            )
        if fan is not None:
            edits.append(
                (
                    "fan",
                    lambda: api.async_set_favourite_fan(self._index, FAN_CHOICES[fan]),
                    lambda fav: int(fav.get("Fan", -1)) == FAN_CHOICES[fan],
                )
            )
        if mode is not None:
            edits.append(
                (
                    "mode",
                    lambda: api.async_set_favourite_mode(
                        self._index, MODE_CHOICES[mode]
                    ),
                    lambda fav: int(fav.get("Mode", -1)) == MODE_CHOICES[mode],
                )
            )

        # The controller answers {ERROR} (or nothing) to a favourite command
        # sent too soon after the last one, so space them out, then judge by
        # reading the favourite back rather than by the replies, and re-send
        # whatever didn't stick.
        pending = [edit for edit in edits if not edit[2](current)]
        last_error: IZoneError | None = None
        for _attempt in range(FAVOURITE_EDIT_ATTEMPTS):
            for _label, send, _kept in pending:
                try:
                    await send()
                except IZoneError as err:
                    last_error = err
                await asyncio.sleep(FAVOURITE_WRITE_SETTLE)
            try:
                self._favourite = await api.async_get_favourite(self._index)
            except IZoneError as err:
                last_error = err
                continue
            pending = [edit for edit in pending if not edit[2](self._favourite)]
            if not pending:
                break

        self.async_write_ha_state()
        if pending:
            detail = f" (last error: {last_error})" if last_error else ""
            raise HomeAssistantError(
                f"iZone favourite '{self._attr_name}' did not keep the change to "
                + ", ".join(label for label, _send, _kept in pending)
                + detail
            )
        _LOGGER.info(
            "iZone favourite '%s' updated: %s",
            self._attr_name,
            {
                k: v
                for k, v in {
                    "zone": zone,
                    "zone_mode": zone_mode,
                    "temperature": temperature,
                    "fan": fan,
                    "mode": mode,
                }.items()
                if v is not None
            },
        )

    def _zone_index(self, zone: str) -> int:
        """A real zone's index, by name (any case) or 1-based number."""
        zones = self.coordinator.data.zones
        wanted = zone.strip()
        by_name = [
            i
            for i, data in enumerate(zones)
            if clean_string(data.get("Name")).casefold() == wanted.casefold()
        ]
        if by_name:
            index = by_name[0]
        elif wanted.isdigit() and 1 <= int(wanted) <= len(zones):
            index = int(wanted) - 1
        else:
            names = ", ".join(clean_string(z.get("Name")) for z in zones)
            raise ServiceValidationError(f"No zone called '{zone}'. Zones: {names}")
        if int(zones[index].get("ZoneType", 0)) == ZoneType.CONSTANT:
            raise ServiceValidationError(
                f"{clean_string(zones[index].get('Name'))} is a constant zone; the "
                "controller manages it"
            )
        return index

    async def _activate_via_controller(
        self, target: dict[str, Any] | None, plan: list[ZoneApply]
    ) -> None:
        """Apply via the single ``FavouriteSet`` command (the fast path)."""
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
                break

            _LOGGER.debug(
                "iZone favourite '%s' not fully applied yet (attempt %d/%d)",
                self._attr_name,
                attempt + 1,
                SCENE_VERIFY_RETRIES + 1,
            )
        else:
            self._log_unverified(target)

        # Keep working on any zone that hasn't landed (or that a sensor
        # dropout means the controller may yet revert) on later polls.
        every_zone = {action.index for action in plan}
        self._track_targets(plan, mode_sent=every_zone, setpoint_sent=every_zone)
        await self.coordinator.async_request_refresh()

    async def _activate_manually(
        self, target: dict[str, Any], plan: list[ZoneApply]
    ) -> None:
        """Apply a favourite zone-by-zone, leaving faulted climate zones for later.

        Reproduces the favourite's per-zone config (and system mode/fan, if it
        specifies them) with individual commands, so a single faulted zone
        can't block the whole scene the way ``FavouriteSet`` does.

        A setpoint is only sent to a zone that's already under climate
        control: the controller discards a setpoint sent to a closed or open
        zone (it just switches the zone to Auto at its *old* setpoint), which
        is how a bedroom once spent the night heating to 22 instead of the
        favourite's 16.5. Those setpoints - and every faulted climate zone -
        are handed to the coordinator, which sends them once the zone is ready.
        """
        api = self.coordinator.api
        zones = self.coordinator.data.zones
        mode_sent: set[int] = set()
        setpoint_sent: set[int] = set()
        try:
            await self._apply_system_settings(target)
            for action in plan:
                if action.defer:
                    continue
                currently_auto = int(zones[action.index].get("Mode", 0)) == ZoneMode.AUTO
                await api.async_set_zone_mode(action.index, ZoneMode(action.mode))
                mode_sent.add(action.index)
                if action.setpoint is not None and currently_auto:
                    await api.async_set_zone_setpoint(
                        action.index, action.setpoint / 100
                    )
                    setpoint_sent.add(action.index)
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            # Track what we meant to do even if a send failed part-way through.
            self._track_targets(plan, mode_sent=mode_sent, setpoint_sent=setpoint_sent)

        _LOGGER.info(
            "iZone favourite '%s' applied per-zone; zone(s) %s waiting for their "
            "sensor, zone(s) %s get their setpoint once climate control is on",
            self._attr_name,
            [a.index for a in plan if a.defer] or "none",
            [
                a.index
                for a in plan
                if a.setpoint is not None and not a.defer and a.index not in setpoint_sent
            ]
            or "none",
        )

        await asyncio.sleep(SCENE_VERIFY_DELAY)
        await self.coordinator.async_refresh()
        if not favourite_target_reached(target, self.coordinator.data.zones):
            self._log_unverified(target)
        await self.coordinator.async_request_refresh()

    def _track_targets(
        self, plan: list[ZoneApply], *, mode_sent: set[int], setpoint_sent: set[int]
    ) -> None:
        """Hand each zone's target to the coordinator to see through."""
        self.coordinator.replace_zone_targets(
            {
                action.index: self.coordinator.new_zone_target(
                    action.mode,
                    action.setpoint,
                    source=self._attr_name,
                    mode_sent=action.index in mode_sent,
                    setpoint_sent=action.index in setpoint_sent,
                )
                for action in plan
            }
        )

    async def _apply_system_settings(self, favourite: dict[str, Any]) -> None:
        """Apply a favourite's system mode/fan, when it specifies them.

        The favourites on real hardware store 0 (unset) for these and only
        drive per-zone config, so this is usually a no-op; it's guarded so an
        unset/unknown value is never sent. The favourite's ``AcSetpoint`` is
        deliberately ignored - its encoding is not the wire setpoint format
        and each climate zone already carries its own setpoint.
        """
        api = self.coordinator.api
        try:
            mode = SysMode(int(favourite.get("Mode", 0)))
        except ValueError:
            mode = None
        if mode is not None:
            await api.async_set_system_mode(mode)
        try:
            fan = SysFan(int(favourite.get("Fan", 0)))
        except ValueError:
            fan = None
        if fan is not None:
            await api.async_set_system_fan(fan)

    def _log_unverified(self, target: dict[str, Any] | None) -> None:
        """Log the specific zones that don't match the favourite (yet)."""
        detail = ""
        if target is not None:
            mismatches = favourite_mismatches(target, self.coordinator.data.zones)
            if mismatches:
                detail = f" - unmatched zones: {mismatches}"
        _LOGGER.info(
            "iZone favourite '%s' not fully applied yet%s - will keep re-applying "
            "the unmatched zones as the controller and their sensors allow",
            self._attr_name,
            detail,
        )
