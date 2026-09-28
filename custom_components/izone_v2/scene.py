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
    SysFan,
    SysMode,
    ZoneApply,
    ZoneMode,
    clean_string,
    favourite_mismatches,
    favourite_target_reached,
    plan_favourite_zones,
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
