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

        # A newer scene supersedes any pending deferrals from an earlier one.
        self.coordinator.clear_deferred_zones()

        # If the favourite drives a climate zone whose sensor is faulted, the
        # controller won't apply the favourite as a unit - apply it ourselves,
        # zone by zone, and defer the faulted climate zones for later.
        plan = (
            plan_favourite_zones(target, self.coordinator.data.zones)
            if target is not None
            else []
        )
        if any(action.defer for action in plan):
            await self._activate_manually(target, plan)
        else:
            await self._activate_via_controller(target)

    async def _activate_via_controller(self, target: dict[str, Any] | None) -> None:
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
                return

            _LOGGER.debug(
                "iZone favourite '%s' not fully applied yet (attempt %d/%d)",
                self._attr_name,
                attempt + 1,
                SCENE_VERIFY_RETRIES + 1,
            )
        else:
            self._log_unverified(target)

        await self.coordinator.async_request_refresh()

    async def _activate_manually(
        self, target: dict[str, Any], plan: list[Any]
    ) -> None:
        """Apply a favourite zone-by-zone, deferring faulted climate zones.

        Reproduces the favourite's per-zone config (and system mode/fan, if it
        specifies them) with individual commands, so a single faulted zone
        can't block the whole scene the way ``FavouriteSet`` does. Faulted
        climate zones are handed to the coordinator to re-apply once their
        sensor recovers.
        """
        api = self.coordinator.api
        deferred: list[int] = []
        try:
            await self._apply_system_settings(target)
            for action in plan:
                if action.defer:
                    self.coordinator.defer_zone_target(
                        action.index, action.mode, action.setpoint
                    )
                    deferred.append(action.index)
                    continue
                await api.async_set_zone_mode(action.index, ZoneMode(action.mode))
                if action.setpoint is not None:
                    await api.async_set_zone_setpoint(
                        action.index, action.setpoint / 100
                    )
        except IZoneError as err:
            raise HomeAssistantError(str(err)) from err

        _LOGGER.info(
            "iZone favourite '%s' applied per-zone; zone(s) %s deferred until "
            "their sensor recovers",
            self._attr_name,
            deferred or "none",
        )

        await asyncio.sleep(SCENE_VERIFY_DELAY)
        await self.coordinator.async_refresh()
        if not favourite_target_reached(target, self.coordinator.data.zones):
            self._log_unverified(target)
        await self.coordinator.async_request_refresh()

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
        """Warn with the specific zones that don't match the favourite."""
        detail = ""
        if target is not None:
            mismatches = favourite_mismatches(target, self.coordinator.data.zones)
            if mismatches:
                detail = f" - unmatched zones: {mismatches}"
        _LOGGER.warning(
            "iZone favourite '%s' could not be confirmed applied%s "
            "(the controller may be busy or a zone sensor faulty)",
            self._attr_name,
            detail,
        )
