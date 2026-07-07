"""Local API client for iZone bridges speaking the V2 JSON API.

Implements the API documented on the iZone Developer Portal
(https://developer.izone.com.au/) and in iZone_JSON_datastrings.h v1.41:

- Discovery:     UDP broadcast ``IASD`` to 255.255.255.255:12107.
                 Bridges reply ``ASPort_12107,Mac_<uid>,IP_<addr>,iZoneV2,...``
- Query:         ``POST http://<bridge>/iZoneRequestV2`` with
                 ``{"iZoneV2Request": {"Type": t, "No": n, "No1": 0}}``
- Control:       ``POST http://<bridge>/iZoneCommandV2`` with the command
                 object, e.g. ``{"SysOn": 1}``. The bridge replies ``OK``.
- Notifications: the bridge broadcasts ``iZoneChanged_System`` /
                 ``iZoneChanged_Zones`` on UDP port 7005 when state changes.

All temperatures on the wire are degrees Celsius multiplied by 100.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DISCOVERY_MSG = b"IASD"
DISCOVERY_PORT = 12107
NOTIFICATION_PORT = 7005
NOTIFICATION_PREFIX = b"iZoneChanged"

REQUEST_TIMEOUT = 10.0

# iZoneV2Request "Type" values (iZone_JSON_datastrings.h v1.41)
REQUEST_SYSTEM = 1  # -> {"SystemV2": {...}}
REQUEST_ZONE = 2  # "No" = zone index -> {"ZonesV2": {...}}
REQUEST_SCHEDULE = 3  # -> {"SchedulesV2": {...}}


class SysMode(IntEnum):
    """SysMode_e - AC unit mode."""

    COOL = 1
    HEAT = 2
    VENT = 3
    DRY = 4
    AUTO = 5
    EXHAUST = 6  # Coolbreeze only (== GasHeat on gas systems)
    PUMP_ONLY = 7  # Coolbreeze only (== ElectricHeat on gas systems)


class SysFan(IntEnum):
    """SysFan_e - AC unit fan speed."""

    LOW = 1
    MED = 2
    HIGH = 3
    AUTO = 4
    TOP = 5
    QUIET = 6
    TURBO = 7


class ZoneType(IntEnum):
    """ZoneType_e - zone configuration."""

    OPEN_CLOSE = 1
    CONSTANT = 2
    AUTO = 3  # temperature (climate) controlled


class ZoneMode(IntEnum):
    """ZoneMode_e - current/commanded zone mode."""

    OPEN = 1
    CLOSE = 2
    AUTO = 3  # climate control
    OVERRIDE = 4
    CONSTANT = 5


class IZoneError(Exception):
    """Base error for the iZone API."""


class IZoneConnectionError(IZoneError):
    """Could not reach the bridge."""


class IZoneCommandError(IZoneError):
    """The bridge rejected a command (non-OK result)."""


@dataclass(frozen=True)
class DiscoveredBridge:
    """A bridge found via UDP discovery."""

    uid: str
    host: str
    supports_v2: bool


def parse_discovery_response(data: bytes) -> DiscoveredBridge | None:
    """Parse an ``ASPort_12107,Mac_...,IP_...`` discovery response."""
    try:
        fields = data.decode("ascii", errors="replace").strip().split(",")
    except UnicodeDecodeError:
        return None
    if (
        len(fields) < 3
        or fields[0] != f"ASPort_{DISCOVERY_PORT}"
        or not fields[1].startswith("Mac_")
        or not fields[2].startswith("IP_")
    ):
        return None
    return DiscoveredBridge(
        uid=fields[1][len("Mac_") :],
        host=fields[2][len("IP_") :],
        supports_v2="iZoneV2" in fields[3:],
    )


async def async_discover(timeout: float = 3.0) -> list[DiscoveredBridge]:
    """Broadcast IASD and collect bridge responses."""
    loop = asyncio.get_running_loop()
    found: dict[str, DiscoveredBridge] = {}

    class _DiscoveryProtocol(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            assert isinstance(transport, asyncio.DatagramTransport)
            transport.sendto(DISCOVERY_MSG, ("255.255.255.255", DISCOVERY_PORT))

        def datagram_received(self, data: bytes, addr: Any) -> None:
            bridge = parse_discovery_response(data)
            if bridge:
                found[bridge.uid] = bridge

        def error_received(self, exc: Exception) -> None:
            _LOGGER.debug("Discovery socket error: %s", exc)

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _DiscoveryProtocol,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    except OSError as err:
        raise IZoneConnectionError(f"Cannot open discovery socket: {err}") from err
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(found.values())


class IZoneApi:
    """Minimal client for the iZone local V2 HTTP API."""

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        self._session = session
        self.host = host
        # The bridge is a small embedded HTTP server and does not tolerate
        # concurrent requests - serialise everything.
        self._lock = asyncio.Lock()

    async def _post(self, path: str, payload: dict[str, Any]) -> str:
        url = f"http://{self.host}/{path}"
        async with self._lock:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with self._session.post(url, json=payload) as resp:
                        resp.raise_for_status()
                        raw = await resp.read()
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                raise IZoneConnectionError(
                    f"Error talking to iZone bridge at {self.host}: {err}"
                ) from err
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # The firmware pads string fields (zone names, tags) with raw
            # 0xFF/0x00 bytes, which is not valid UTF-8. latin-1 maps every
            # byte, so decoding can't fail; padding is stripped later.
            return raw.decode("latin-1")

    async def async_request(self, req_type: int, no: int = 0) -> dict[str, Any]:
        """POST /iZoneRequestV2 and return the parsed JSON response."""
        text = await self._post(
            "iZoneRequestV2",
            {"iZoneV2Request": {"Type": req_type, "No": no, "No1": 0}},
        )
        try:
            # strict=False tolerates raw control characters (e.g. NUL padding
            # inside name strings) that the firmware emits.
            return json.loads(text, strict=False)
        except json.JSONDecodeError as err:
            raise IZoneError(
                f"Invalid JSON from bridge {self.host}: {text[:200]!r}"
            ) from err

    async def async_get_system(self) -> dict[str, Any]:
        """Return the full system response (incl. AirStreamDeviceUId)."""
        data = await self.async_request(REQUEST_SYSTEM)
        if "SystemV2" not in data:
            raise IZoneError(
                f"Bridge {self.host} did not return SystemV2 - "
                "it may only support the legacy v1 API"
            )
        return data

    async def async_get_zone(self, index: int) -> dict[str, Any]:
        """Return the ZonesV2 datagram for a zone (0-based index)."""
        data = await self.async_request(REQUEST_ZONE, index)
        try:
            return data["ZonesV2"]
        except KeyError as err:
            raise IZoneError(
                f"Bridge {self.host} returned no ZonesV2 for zone {index}"
            ) from err

    async def async_command(self, payload: dict[str, Any]) -> None:
        """POST a command to /iZoneCommandV2 and verify the OK result."""
        text = (await self._post("iZoneCommandV2", payload)).strip()
        # Documented results: OK, InvalidRequest, InvalidUser,
        # UserNotAllowed, Error.
        if text != "OK" and '"OK"' not in text:
            raise IZoneCommandError(
                f"Bridge {self.host} rejected command {payload}: {text[:200]!r}"
            )

    # -- convenience command wrappers -------------------------------------

    async def async_set_system_on(self, on: bool) -> None:
        await self.async_command({"SysOn": 1 if on else 0})

    async def async_set_system_mode(self, mode: SysMode) -> None:
        await self.async_command({"SysMode": int(mode)})

    async def async_set_system_fan(self, fan: SysFan) -> None:
        await self.async_command({"SysFan": int(fan)})

    async def async_set_system_setpoint(self, temp_c: float) -> None:
        # Wire format is degrees x100, limits 1500-3000 per the spec; the
        # caller is expected to clamp to the system's Eco limits.
        await self.async_command({"SysSetpoint": int(round(temp_c * 100))})

    async def async_set_zone_mode(self, index: int, mode: ZoneMode) -> None:
        await self.async_command({"ZoneMode": {"Index": index, "Mode": int(mode)}})

    async def async_set_zone_setpoint(self, index: int, temp_c: float) -> None:
        await self.async_command(
            {"ZoneSetpoint": {"Index": index, "Setpoint": int(round(temp_c * 100))}}
        )
