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
from collections.abc import Callable
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
COMMAND_BUSY_RETRIES = 6
COMMAND_BUSY_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0)

# Retry behaviour for transient connection failures (timeout / reset /
# refused). The bridge is a single-connection embedded server that
# intermittently drops requests, especially right after a command while it
# is actuating dampers - a single dropped poll shouldn't knock every entity
# offline, so give it a couple of quick retries first.
CONNECT_RETRIES = 2
CONNECT_RETRY_DELAYS = (0.5, 1.5)

# HTTP endpoints.
REQUEST_ENDPOINT = "iZoneRequestV2"
COMMAND_ENDPOINT = "iZoneCommandV2"

# Settle time held (with the request lock) after each command, so a burst of
# commands - e.g. an HA scene setting every zone at once - is paced instead of
# hammering the single-connection controller into a {BUSY} cascade. Reads
# (polls) are not paced.
COMMAND_SETTLE_DELAY = 0.3

# iZoneV2Request "Type" values (iZone_JSON_datastrings.h v1.41)
REQUEST_SYSTEM = 1  # -> {"SystemV2": {...}}
REQUEST_ZONE = 2  # "No" = zone index -> {"ZonesV2": {...}}
REQUEST_SCHEDULE = 3  # "No" = favourite index -> {"SchedulesV2": {...}}

# FavouriteSet accepts 1-9 (wire value = 0-based index + 1); documented on
# https://developer.izone.com.au/docs/reference/schedule/
FAVOURITE_COUNT = 9


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


def _err_str(err: Exception | None) -> str:
    """A human-readable message for an exception.

    Several exceptions we catch (notably asyncio.TimeoutError) stringify to
    an empty string, which produced useless "Error talking to bridge: "
    log lines - fall back to the class name so the cause is visible.
    """
    if err is None:
        return "unknown error"
    return str(err) or type(err).__name__


def favourite_target_reached(
    favourite: dict[str, Any], zones: list[dict[str, Any]]
) -> bool:
    """True if live zones match a favourite's stored per-zone config.

    Used to confirm a favourite ("scene") actually applied. Compares zone
    Mode for every real zone, plus Setpoint for zones the favourite drives
    in climate (Auto) mode - a closed zone keeps its own setpoint, so
    comparing it there would give false mismatches. Constant/bypass zones
    (controller-managed) and zones reporting a sensor fault (can't be
    verified) are skipped.
    """
    fav_zones = favourite.get("Zones") or []
    for index, zone in enumerate(zones):
        if index >= len(fav_zones):
            break
        if int(zone.get("ZoneType", 0)) == ZoneType.CONSTANT:
            continue
        if zone.get("SensorFault"):
            continue
        target = fav_zones[index]
        target_mode = int(target.get("Mode", -2))
        if int(zone.get("Mode", -1)) != target_mode:
            return False
        if target_mode == ZoneMode.AUTO and int(zone.get("Setpoint", -1)) != int(
            target.get("Setpoint", -2)
        ):
            return False
    return True


@dataclass(frozen=True)
class ZoneApply:
    """One zone's target when applying a favourite ("scene") zone-by-zone.

    `setpoint` is a wire temperature (Celsius x100) and is only meaningful
    for climate (Auto) targets; it is None otherwise. `defer` is True when
    the target needs the zone's sensor (Auto mode) but that sensor is
    currently faulted - such a zone can't be driven now and must be applied
    later, once the sensor recovers.
    """

    index: int
    mode: int
    setpoint: int | None
    defer: bool


def plan_favourite_zones(
    favourite: dict[str, Any], zones: list[dict[str, Any]]
) -> list[ZoneApply]:
    """Per-zone actions that reproduce a favourite without ``FavouriteSet``.

    Used as a fallback when the controller won't apply a favourite as a unit
    because one of its climate zones has a faulted sensor. Constant/bypass
    zones are controller-managed and skipped. Open/Close targets never need a
    sensor, so a faulted zone is still applied for those; only an Auto target
    on a faulted zone is deferred.
    """
    fav_zones = favourite.get("Zones") or []
    plan: list[ZoneApply] = []
    for index, zone in enumerate(zones):
        if index >= len(fav_zones):
            break
        if int(zone.get("ZoneType", 0)) == ZoneType.CONSTANT:
            continue
        mode = int(fav_zones[index].get("Mode", 0))
        if mode not in (ZoneMode.OPEN, ZoneMode.CLOSE, ZoneMode.AUTO):
            continue  # unset / unknown target, leave the zone alone
        is_auto = mode == ZoneMode.AUTO
        setpoint = int(fav_zones[index].get("Setpoint", 0)) if is_auto else None
        defer = is_auto and bool(zone.get("SensorFault"))
        plan.append(ZoneApply(index=index, mode=mode, setpoint=setpoint, defer=defer))
    return plan


def favourite_mismatches(
    favourite: dict[str, Any], zones: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Human-readable list of why a favourite isn't (fully) applied.

    A diagnostic companion to :func:`favourite_target_reached`: instead of a
    bare True/False it names each zone that doesn't match and why, so an
    unconfirmed scene can be logged as e.g. "zone 2 sensor_fault, zone 7 mode
    want 3 got 2" rather than a generic warning. Constant zones are skipped.
    """
    fav_zones = favourite.get("Zones") or []
    out: list[dict[str, Any]] = []
    for index, zone in enumerate(zones):
        if index >= len(fav_zones):
            break
        if int(zone.get("ZoneType", 0)) == ZoneType.CONSTANT:
            continue
        if zone.get("SensorFault"):
            out.append({"zone": index, "reason": "sensor_fault"})
            continue
        target = fav_zones[index]
        want_mode = int(target.get("Mode", -2))
        got_mode = int(zone.get("Mode", -1))
        if got_mode != want_mode:
            out.append(
                {"zone": index, "reason": "mode", "want": want_mode, "got": got_mode}
            )
            continue
        if want_mode == ZoneMode.AUTO:
            want_sp = int(target.get("Setpoint", -2))
            got_sp = int(zone.get("Setpoint", -1))
            if got_sp != want_sp:
                out.append(
                    {"zone": index, "reason": "setpoint", "want": want_sp, "got": got_sp}
                )
    return out


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
        # Optional hook, invoked with True after a command succeeds and False
        # after one fails (busy-exhaustion or connection error). The
        # coordinator uses this to track bridge-overload health.
        self.on_command_result: Callable[[bool], None] | None = None

    async def _post(self, path: str, payload: dict[str, Any]) -> str:
        url = f"http://{self.host}/{path}"
        async with self._lock:
            raw = await self._request_raw(url, payload)
            # Pace commands: keep the lock a moment after a write so queued
            # commands (an HA scene setting every zone) are spaced out rather
            # than hammering the controller into a {BUSY} cascade.
            if path == COMMAND_ENDPOINT and COMMAND_SETTLE_DELAY:
                await asyncio.sleep(COMMAND_SETTLE_DELAY)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # The firmware pads string fields (zone names, tags) with raw
            # 0xFF/0x00 bytes, which is not valid UTF-8. latin-1 maps every
            # byte, so decoding can't fail; padding is stripped later.
            return raw.decode("latin-1")

    async def _request_raw(self, url: str, payload: dict[str, Any]) -> bytes:
        """POST with retries for transient connection failures.

        The lock is held by the caller, so retries stay serialised. All the
        commands we send are idempotent (SysOn/SysMode/ZoneMode/FavouriteSet
        set an absolute target), so re-sending after a timeout can't cause a
        double-toggle.
        """
        last_err: Exception | None = None
        for attempt in range(CONNECT_RETRIES + 1):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with self._session.post(url, json=payload) as resp:
                        resp.raise_for_status()
                        return await resp.read()
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                last_err = err
                if attempt < CONNECT_RETRIES:
                    _LOGGER.debug(
                        "Request to %s failed (%s), retry %d/%d",
                        url,
                        _err_str(err),
                        attempt + 1,
                        CONNECT_RETRIES,
                    )
                    await asyncio.sleep(CONNECT_RETRY_DELAYS[attempt])
        raise IZoneConnectionError(
            f"Error talking to iZone bridge at {self.host}: {_err_str(last_err)}"
        ) from last_err

    async def async_request(self, req_type: int, no: int = 0) -> dict[str, Any]:
        """POST /iZoneRequestV2 and return the parsed JSON response."""
        text = await self._post(
            REQUEST_ENDPOINT,
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

    async def async_get_favourite(self, index: int) -> dict[str, Any]:
        """Return the Schedule/Favourite datagram for a saved favourite.

        0-based index (0-8); a favourite with an empty Name is an unused
        slot.
        """
        data = await self.async_request(REQUEST_SCHEDULE, index)
        try:
            return data["SchedulesV2"]
        except KeyError as err:
            raise IZoneError(
                f"Bridge {self.host} returned no SchedulesV2 for favourite {index}"
            ) from err

    async def async_command(self, payload: dict[str, Any]) -> None:
        """Send a command, reporting success/failure for health tracking."""
        try:
            await self._send_command(payload)
        except IZoneError:
            self._report_command_result(ok=False)
            raise
        self._report_command_result(ok=True)

    def _report_command_result(self, ok: bool) -> None:
        if self.on_command_result is not None:
            self.on_command_result(ok)

    async def _send_command(self, payload: dict[str, Any]) -> None:
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
            text = (await self._post(COMMAND_ENDPOINT, payload)).strip()
            _LOGGER.debug("Bridge %s command %s -> %r", self.host, payload, text)
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

    async def async_execute_favourite(self, index: int) -> None:
        """Trigger a saved favourite (a "scene" in the iZone app) to apply.

        0-based index (0-8); the documented "Start Schedule Manually"
        command takes a 1-based value (1-9).
        """
        await self.async_command({"FavouriteSet": index + 1})
