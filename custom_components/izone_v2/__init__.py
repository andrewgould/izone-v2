"""The iZone V2 (Local API) integration."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NOTIFICATION_PORT, NOTIFICATION_PREFIX, IZoneApi
from .const import CONF_HOST
from .coordinator import IZoneConfigEntry, IZoneCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    """Set up an iZone bridge from a config entry."""
    api = IZoneApi(async_get_clientsession(hass), entry.data[CONF_HOST])
    coordinator = IZoneCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # The bridge broadcasts iZoneChanged_System / iZoneChanged_Zones on
    # UDP 7005 whenever state changes (and every 120s regardless). Listen
    # for those to refresh promptly; polling remains as the fallback.
    transport = await _async_start_notification_listener(hass, coordinator)
    if transport is not None:
        entry.async_on_unload(transport.close)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_start_notification_listener(
    hass: HomeAssistant, coordinator: IZoneCoordinator
) -> asyncio.DatagramTransport | None:
    """Listen for change-notification broadcasts on UDP 7005."""

    class _NotificationProtocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            if data.startswith(NOTIFICATION_PREFIX):
                _LOGGER.debug("Change notification %s from %s", data, addr)
                # async_request_refresh is debounced by the coordinator.
                hass.async_create_task(coordinator.async_request_refresh())

        def error_received(self, exc: Exception) -> None:
            _LOGGER.debug("Notification socket error: %s", exc)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setblocking(False)
    try:
        sock.bind(("0.0.0.0", NOTIFICATION_PORT))
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            _NotificationProtocol, sock=sock
        )
    except OSError as err:
        _LOGGER.info(
            "Could not listen for iZone change notifications on UDP %s (%s); "
            "falling back to polling only",
            NOTIFICATION_PORT,
            err,
        )
        sock.close()
        return None
    return transport
