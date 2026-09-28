"""Data update coordinator for the iZone V2 integration."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    IZoneApi,
    IZoneError,
    TargetStep,
    ZoneMode,
    ZoneTarget,
    clean_string,
    next_target_step,
)
from .const import (
    COMMAND_FAILURE_WINDOW,
    DOMAIN,
    OVERLOAD_THRESHOLD,
    POLL_INTERVAL,
    ZONE_TARGET_EXPIRY,
    ZONE_TARGET_MAX_SENDS,
    ZONE_TARGET_RESEND,
)

_LOGGER = logging.getLogger(__name__)

type IZoneConfigEntry = ConfigEntry[IZoneCoordinator]


@dataclass
class IZoneData:
    """State snapshot of an iZone system."""

    uid: str
    system: dict[str, Any]  # SystemV2 datagram
    zones: list[dict[str, Any]]  # ZonesV2 datagrams, index == zone index


class IZoneCoordinator(DataUpdateCoordinator[IZoneData]):
    """Polls the bridge over the V2 local API."""

    config_entry: IZoneConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: IZoneConfigEntry, api: IZoneApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.api = api
        # Monotonic timestamps of recent failed commands, for the
        # bridge-overload signal. Bounded so a runaway can't grow unbounded.
        self._command_failures: deque[float] = deque(maxlen=64)
        api.on_command_result = self._note_command_result
        # Zone states we've asked for but haven't seen yet, keyed by zone
        # index. Checked and nudged along on every poll until confirmed (see
        # _reconcile_zone_targets).
        self._zone_targets: dict[int, ZoneTarget] = {}

    @callback
    def _note_command_result(self, ok: bool) -> None:
        """Record a command outcome and push the health state to listeners."""
        now = self.hass.loop.time()
        if not ok:
            self._command_failures.append(now)
        self._prune_failures(now)
        self.async_update_listeners()

    def _prune_failures(self, now: float) -> None:
        while self._command_failures and (
            now - self._command_failures[0] > COMMAND_FAILURE_WINDOW
        ):
            self._command_failures.popleft()

    @property
    def recent_command_failures(self) -> int:
        """Number of failed commands within the recent window."""
        self._prune_failures(self.hass.loop.time())
        return len(self._command_failures)

    @property
    def bridge_overloaded(self) -> bool:
        """True when commands are failing often enough to warrant action."""
        return self.recent_command_failures >= OVERLOAD_THRESHOLD

    # -- pending zone targets ----------------------------------------------

    def new_zone_target(
        self,
        mode: int,
        setpoint: int | None,
        *,
        source: str,
        mode_sent: bool = False,
        setpoint_sent: bool = False,
    ) -> ZoneTarget:
        """A target starting now; `*_sent` = that part was just commanded."""
        now = self.hass.loop.time()
        return ZoneTarget(
            mode=mode,
            setpoint=setpoint,
            expires=now + ZONE_TARGET_EXPIRY,
            source=source,
            mode_sent=now if mode_sent else None,
            setpoint_sent=now if setpoint_sent else None,
        )

    def replace_zone_targets(self, targets: dict[int, ZoneTarget]) -> None:
        """Swap in a new set of targets - a newer scene supersedes an older one."""
        self._zone_targets = dict(targets)
        self.async_update_listeners()

    def set_zone_target(self, index: int, target: ZoneTarget) -> None:
        """Track a target for a single zone."""
        self._zone_targets[index] = target
        self.async_update_listeners()

    def cancel_zone_target(self, index: int) -> None:
        """Stop working towards a zone's target (a newer command supersedes it)."""
        if self._zone_targets.pop(index, None) is not None:
            self.async_update_listeners()

    @property
    def zone_targets(self) -> dict[int, ZoneTarget]:
        """Pending zone targets, keyed by zone index (read-only view)."""
        return dict(self._zone_targets)

    async def _reconcile_zone_targets(self, zones: list[dict[str, Any]]) -> None:
        """Nudge each pending zone target along, and retire finished ones.

        Runs inside the poll (so it's serialised with reads). A failed send is
        just logged - the target stays pending and is retried after the resend
        interval, rather than failing the poll.
        """
        if not self._zone_targets:
            return
        now = self.hass.loop.time()
        by_index = {int(z.get("Index", -1)): z for z in zones}
        for index, target in list(self._zone_targets.items()):
            zone = by_index.get(index)
            if zone is None:
                continue
            step = next_target_step(
                target,
                zone,
                now,
                resend_after=ZONE_TARGET_RESEND,
                max_sends=ZONE_TARGET_MAX_SENDS,
            )
            if step is TargetStep.WAIT:
                continue
            name = f"{clean_string(zone.get('Name')) or 'Zone'} (zone {index})"
            if step in (TargetStep.SEND_MODE, TargetStep.SEND_SETPOINT):
                await self._send_zone_target(name, index, target, step, now)
                continue

            del self._zone_targets[index]
            if step is TargetStep.REACHED:
                how = [
                    *(["once its sensor recovered"] if target.waited_for_sensor else []),
                    *([f"after {target.sends} follow-up command(s)"] if target.sends else []),
                ]
                if how:
                    _LOGGER.info(
                        "%s reached its '%s' target %s",
                        name,
                        target.source,
                        ", ".join(how),
                    )
            elif step is TargetStep.OVERRIDDEN:
                _LOGGER.info(
                    "%s was changed outside '%s' (now mode %s, setpoint %s) - "
                    "leaving it as set",
                    name,
                    target.source,
                    zone.get("Mode"),
                    zone.get("Setpoint"),
                )
            elif step is TargetStep.EXPIRED:
                _LOGGER.info(
                    "Stopped waiting for %s to take its '%s' target (%s)",
                    name,
                    target.source,
                    "its sensor never recovered"
                    if zone.get("SensorFault")
                    else "timed out",
                )
            else:  # EXHAUSTED
                _LOGGER.warning(
                    "%s still hasn't taken its '%s' target (mode %s, setpoint %s) "
                    "after %d follow-up commands - giving up; it reads mode %s, setpoint %s",
                    name,
                    target.source,
                    target.mode,
                    target.setpoint,
                    target.sends,
                    zone.get("Mode"),
                    zone.get("Setpoint"),
                )

    async def _send_zone_target(
        self, name: str, index: int, target: ZoneTarget, step: TargetStep, now: float
    ) -> None:
        """Send the missing part of a zone target, recording the attempt."""
        target.sends += 1
        try:
            if step is TargetStep.SEND_MODE:
                target.mode_sent = now
                await self.api.async_set_zone_mode(index, ZoneMode(target.mode))
            else:
                target.setpoint_sent = now
                await self.api.async_set_zone_setpoint(
                    index, (target.setpoint or 0) / 100
                )
        except IZoneError as err:
            _LOGGER.debug(
                "Sending %s's '%s' target failed, will retry: %s",
                name,
                target.source,
                err,
            )
            return
        _LOGGER.debug(
            "%s: sent %s for its '%s' target (follow-up %d)",
            name,
            "mode" if step is TargetStep.SEND_MODE else "setpoint",
            target.source,
            target.sends,
        )

    async def _async_update_data(self) -> IZoneData:
        try:
            response = await self.api.async_get_system()
            system = response["SystemV2"]
            # Zones must be fetched one at a time (Type 2, No = index) and
            # sequentially - the bridge can't handle concurrent requests.
            zones = [
                await self.api.async_get_zone(index)
                for index in range(int(system.get("NoOfZones", 0)))
            ]
            await self._reconcile_zone_targets(zones)
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        return IZoneData(
            uid=str(response.get("AirStreamDeviceUId", "")), system=system, zones=zones
        )
