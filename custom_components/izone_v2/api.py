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

# Retry behaviour for '{BUSY}' command replies (bridge still actuating a
# previous command, e.g. a damper motor). Delays are spaced out rather than
# tight-looped since BUSY tends to clear after the physical actuation
# finishes, not immediately.
COMMAND_BUSY_RETRIES = 5
COMMAND_BUSY_DELAYS = (0.5, 1.0, 2.0, 4.0, 6.0)

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


# Room sensor types (RoomSensorType_t) we care about
ROOM_SENSOR_WIRELESS = 3  # RoomSensorCRFS - battery powered
ROOM_SENSOR_NONE = 255


def clean_string(value: Any) -> str:
    """Strip firmware padding (NUL / 0xFF bytes) from a wire string."""
    if value is None:
        return ""
    # Names are fixed-size buffers; content ends at the first NUL and any
    # remaining bytes are 0xFF padding (decoded as 'ÿ' via latin-1).
    return str(value).split("\x00", 1)[0].strip("\xff \t\r\n")


def temp_from_wire(value: Any) -> float | None:
    """Convert a wire temperature (Celsius x100) to degrees, if plausible."""
    try:
        temp = int(value) / 100
    except (TypeError, ValueError):
        return None
    # Zones without a sensor report 0 / garbage.
    if temp < -20 or temp > 60 or temp == 0:
        return None
    return temp


class IZoneError(Exception):
    """Base error for the iZone API."""


class IZoneConnectionError(IZoneError):
    """Could not reach the bridge."""


class IZoneCommandError(IZoneError):
    """The bridge rejected a command (non-OK result)."""


class IZoneBusyError(IZoneCommandError):
    """The bridge stayed busy through every retry."""


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
        """POST a command to /iZoneCommandV2 and verify the OK result.

        Documented results: OK, InvalidRequest, InvalidUser, UserNotAllowed,
        Error. Real bridges reply with v1-style bracing/quoting variants
        ('{OK}', '"OK"'), so normalise before comparing. Bridges also reply
        '{BUSY}' while still actuating a previous command (e.g. a damper
        motor mid-travel) - this is transient, not a rejection, so retry
        with backoff instead of failing the whole action (scenes commonly
        trigger several zone commands back-to-back and hit this).
        """
        last_reply = ""
        for attempt in range(COMMAND_BUSY_RETRIES + 1):
            text = (await self._post("iZoneCommandV2", payload)).strip()
            normalised = text.strip('{}" \t\r\n')
            if normalised == "OK":
                return
            last_reply = text
            if normalised != "BUSY":
                raise IZoneCommandError(
                    f"Bridge {self.host} rejected command {payload}: {text[:200]!r}"
                )
            if attempt < COMMAND_BUSY_RETRIES:
                _LOGGER.debug(
                    "Bridge %s busy, retrying command %s in %.1fs (attempt %d/%d)",
                    self.host,
                    payload,
                    COMMAND_BUSY_DELAYS[attempt],
                    attempt + 1,
                    COMMAND_BUSY_RETRIES,
                )
                await asyncio.sleep(COMMAND_BUSY_DELAYS[attempt])
        raise IZoneBusyError(
            f"Bridge {self.host} still busy after {COMMAND_BUSY_RETRIES} retries "
            f"for command {payload}: {last_reply[:200]!r}"
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
        await self.async_command({"SysSetpoint": round(temp_c * 100)})

    async def async_set_zone_mode(self, index: int, mode: ZoneMode) -> None:
        await self.async_command({"ZoneMode": {"Index": index, "Mode": int(mode)}})

    async def async_set_zone_setpoint(self, index: int, temp_c: float) -> None:
        await self.async_command(
            {"ZoneSetpoint": {"Index": index, "Setpoint": round(temp_c * 100)}}
        )

    async def async_set_sleep_timer(self, minutes: int) -> None:
        """Set the sleep timer in minutes; 0 turns it off."""
        await self.async_command({"SysSleepTimer": int(minutes)})

    async def async_set_isave(self, on: bool) -> None:
        await self.async_command({"iSaveOn": 1 if on else 0})

    async def async_set_zone_min_air(self, index: int, percent: int) -> None:
        """Zone minimum damper opening, 0-100 in steps of 5."""
        await self.async_command({"ZoneMinAir": {"Index": index, "MinAir": percent}})

    async def async_set_zone_max_air(self, index: int, percent: int) -> None:
        """Zone maximum damper opening, 0-100 in steps of 5."""
        await self.async_command({"ZoneMaxAir": {"Index": index, "MaxAir": percent}})
