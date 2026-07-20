"""Protocol tests for the iZone V2 API client, run against a mock bridge."""

from __future__ import annotations

import izone_api as api
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

# --- pure helpers ----------------------------------------------------------


def test_parse_discovery_v2() -> None:
    result = api.parse_discovery_response(
        b"ASPort_12107,Mac_000013170,IP_192.168.1.5,iZoneV2,iLight,X,iPower,Split"
    )
    assert result is not None
    assert result.uid == "000013170"
    assert result.host == "192.168.1.5"
    assert result.supports_v2


def test_parse_discovery_v1_only() -> None:
    result = api.parse_discovery_response(
        b"ASPort_12107,Mac_000099999,IP_10.0.0.2,iZone"
    )
    assert result is not None
    assert not result.supports_v2


@pytest.mark.parametrize(
    "data",
    [b"garbage", b"ASPort_12107,Mac_000013170", b"", b"\xff\xfe\x00"],
)
def test_parse_discovery_rejects_invalid(data: bytes) -> None:
    assert api.parse_discovery_response(data) is None


def test_clean_string_strips_firmware_padding() -> None:
    # NUL terminator followed by 0xFF padding, decoded via latin-1
    assert api.clean_string("Living\x00\xff\xff\xff\xff") == "Living"
    assert api.clean_string("  Bedroom 1 ") == "Bedroom 1"
    assert api.clean_string(None) == ""
    assert api.clean_string("\x00\xff\xff") == ""


def test_temp_from_wire() -> None:
    assert api.temp_from_wire(2250) == 22.5
    assert api.temp_from_wire(0) is None  # no sensor
    assert api.temp_from_wire(None) is None
    assert api.temp_from_wire("bogus") is None
    assert api.temp_from_wire(9900) is None  # implausible


def test_enums_match_official_spec() -> None:
    assert int(api.SysMode.COOL) == 1
    assert int(api.SysMode.AUTO) == 5
    assert int(api.SysFan.AUTO) == 4
    assert int(api.SysFan.TOP) == 5
    assert int(api.ZoneMode.OPEN) == 1
    assert int(api.ZoneMode.CLOSE) == 2
    assert int(api.ZoneMode.AUTO) == 3
    assert int(api.ZoneType.AUTO) == 3


# --- mock bridge -----------------------------------------------------------

UID = "000013170"

# Real firmware pads fixed-size string buffers with NUL + 0xFF bytes and the
# body is not valid UTF-8 - this reproduces the decode bug found in the field.
SYSTEM_BODY = (
    b'{"AirStreamDeviceUId":"000013170","DeviceType":"ASH",'
    b'"SystemV2":{"SysOn":1,"SysMode":1,"SysFan":2,"Setpoint":2250,'
    b'"Temp":2380,"Supply":1520,"NoOfZones":2,"SleepTimer":0,'
    b'"Tag1":"My House\x00\xff\xff\xff\xff"}}'
)
ZONE_BODIES = [
    (
        b'{"AirStreamDeviceUId":"000013170","DeviceType":"ASH",'
        b'"ZonesV2":{"Index":0,"Name":"Living\x00\xff\xff\xff\xff\xff\xff",'
        b'"ZoneType":3,"Mode":3,"Setpoint":2200,"Temp":2310,"SensorFault":0}}'
    ),
    (
        b'{"AirStreamDeviceUId":"000013170","DeviceType":"ASH",'
        b'"ZonesV2":{"Index":1,"Name":"Garage\x00\xff\xff\xff\xff\xff\xff",'
        b'"ZoneType":1,"Mode":2,"Setpoint":0,"Temp":0,"SensorFault":0}}'
    ),
]
# Favourite 0 is a configured scene; favourite 1 is an unused slot (empty
# name, per the official Schedule reference datagram).
FAVOURITE_BODIES = [
    (
        b'{"AirStreamDeviceUId":"000013170","DeviceType":"ASH",'
        b'"SchedulesV2":{"Index":0,"Name":"Movie Night\x00\xff\xff\xff",'
        b'"Enabled":0,"Mode":1,"Fan":2,"StartH":31,"StartM":63,'
        b'"StopH":31,"StopM":63}}'
    ),
    (
        b'{"AirStreamDeviceUId":"000013170","DeviceType":"ASH",'
        b'"SchedulesV2":{"Index":1,"Name":"\x00\xff\xff\xff\xff\xff\xff\xff",'
        b'"Enabled":0,"Mode":0,"Fan":0,"StartH":31,"StartM":63,'
        b'"StopH":31,"StopM":63}}'
    ),
]


class MockBridge:
    """A fake iZone bridge with firmware-realistic quirks."""

    def __init__(
        self,
        command_reply: bytes | list[bytes] = b"{OK}",
        command_fail_times: int = 0,
    ) -> None:
        # Real bridges reply '{OK}', not the documented bare 'OK'. A list
        # is consumed one reply per command call, holding the last entry
        # once exhausted (for simulating BUSY-then-OK sequences).
        self._replies = (
            list(command_reply) if isinstance(command_reply, list) else None
        )
        self.command_reply = command_reply
        # Return HTTP 503 for the first N command calls, to simulate the
        # bridge transiently dropping requests.
        self._command_fail_times = command_fail_times
        self.commands: list[dict] = []
        self.command_calls = 0
        app = web.Application()
        app.router.add_post("/iZoneRequestV2", self._request)
        app.router.add_post("/iZoneCommandV2", self._command)
        self.server = TestServer(app)

    async def _request(self, request: web.Request) -> web.Response:
        query = (await request.json())["iZoneV2Request"]
        if query["Type"] == api.REQUEST_SYSTEM:
            body = SYSTEM_BODY
        elif query["Type"] == api.REQUEST_ZONE:
            body = ZONE_BODIES[query["No"]]
        elif query["Type"] == api.REQUEST_SCHEDULE:
            body = FAVOURITE_BODIES[query["No"]]
        else:
            return web.Response(text="InvalidRequest")
        return web.Response(body=body, content_type="application/json")

    async def _command(self, request: web.Request) -> web.Response:
        self.command_calls += 1
        if self._command_fail_times > 0:
            self._command_fail_times -= 1
            return web.Response(status=503)
        assert request.content_type == "application/json"
        self.commands.append(await request.json())
        if self._replies is not None:
            reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
            return web.Response(body=reply)
        return web.Response(body=self.command_reply)

    @property
    def host(self) -> str:
        return f"{self.server.host}:{self.server.port}"


@pytest.fixture
async def bridge():
    mock = MockBridge()
    await mock.server.start_server()
    yield mock
    await mock.server.close()


async def test_query_decodes_non_utf8_body(bridge: MockBridge) -> None:
    async with ClientSession() as session:
        client = api.IZoneApi(session, bridge.host)
        system = await client.async_get_system()
        assert system["AirStreamDeviceUId"] == UID
        assert system["SystemV2"]["Setpoint"] == 2250

        zone = await client.async_get_zone(0)
        assert api.clean_string(zone["Name"]) == "Living"
        assert zone["ZoneType"] == api.ZoneType.AUTO

        favourite = await client.async_get_favourite(0)
        assert api.clean_string(favourite["Name"]) == "Movie Night"
        empty_slot = await client.async_get_favourite(1)
        assert api.clean_string(empty_slot["Name"]) == ""


async def test_execute_favourite_sends_one_based_index(bridge: MockBridge) -> None:
    async with ClientSession() as session:
        client = api.IZoneApi(session, bridge.host)
        await client.async_execute_favourite(0)
        await client.async_execute_favourite(8)
    assert bridge.commands == [{"FavouriteSet": 1}, {"FavouriteSet": 9}]


async def test_commands_match_official_spec(bridge: MockBridge) -> None:
    async with ClientSession() as session:
        client = api.IZoneApi(session, bridge.host)
        await client.async_set_system_on(True)
        await client.async_set_system_mode(api.SysMode.COOL)
        await client.async_set_system_fan(api.SysFan.AUTO)
        await client.async_set_system_setpoint(22.5)
        await client.async_set_zone_mode(1, api.ZoneMode.OPEN)
        await client.async_set_zone_setpoint(0, 21.0)
        await client.async_set_sleep_timer(60)
        await client.async_set_isave(True)
        await client.async_set_zone_min_air(0, 20)
        await client.async_set_zone_max_air(0, 90)
    assert bridge.commands == [
        {"SysOn": 1},
        {"SysMode": 1},
        {"SysFan": 4},
        {"SysSetpoint": 2250},
        {"ZoneMode": {"Index": 1, "Mode": 1}},
        {"ZoneSetpoint": {"Index": 0, "Setpoint": 2100}},
        {"SysSleepTimer": 60},
        {"iSaveOn": 1},
        {"ZoneMinAir": {"Index": 0, "MinAir": 20}},
        {"ZoneMaxAir": {"Index": 0, "MaxAir": 90}},
    ]


@pytest.mark.parametrize("reply", [b"OK", b"{OK}", b'"OK"', b" OK \r\n"])
async def test_command_accepts_ok_variants(reply: bytes) -> None:
    mock = MockBridge(command_reply=reply)
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            await client.async_command({"SysOn": 1})  # must not raise
    finally:
        await mock.server.close()


@pytest.mark.parametrize(
    "reply", [b"InvalidRequest", b"Error", b"UserNotAllowed", b""]
)
async def test_command_rejects_errors(reply: bytes) -> None:
    mock = MockBridge(command_reply=reply)
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            with pytest.raises(api.IZoneCommandError):
                await client.async_command({"SysOn": 1})
    finally:
        await mock.server.close()


async def test_command_retries_through_busy_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(api, "COMMAND_BUSY_DELAYS", (0, 0, 0, 0, 0))
    mock = MockBridge(command_reply=[b"{BUSY}", b"{BUSY}", b"{OK}"])
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            await client.async_command({"ZoneMode": {"Index": 1, "Mode": 3}})
        assert len(mock.commands) == 3
    finally:
        await mock.server.close()


async def test_command_raises_busy_error_after_exhausting_retries(monkeypatch) -> None:
    monkeypatch.setattr(api, "COMMAND_BUSY_DELAYS", (0,) * api.COMMAND_BUSY_RETRIES)
    mock = MockBridge(command_reply=b"{BUSY}")
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            with pytest.raises(api.IZoneBusyError):
                await client.async_command({"ZoneMode": {"Index": 1, "Mode": 3}})
        assert isinstance(api.IZoneBusyError("x"), api.IZoneCommandError)
        assert len(mock.commands) == api.COMMAND_BUSY_RETRIES + 1
    finally:
        await mock.server.close()


async def test_command_result_callback_reports_success(bridge: MockBridge) -> None:
    results: list[bool] = []
    async with ClientSession() as session:
        client = api.IZoneApi(session, bridge.host)
        client.on_command_result = results.append
        await client.async_command({"SysOn": 1})
    assert results == [True]


async def test_command_result_callback_reports_busy_failure(monkeypatch) -> None:
    monkeypatch.setattr(api, "COMMAND_BUSY_DELAYS", (0,) * api.COMMAND_BUSY_RETRIES)
    results: list[bool] = []
    mock = MockBridge(command_reply=b"{BUSY}")
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            client.on_command_result = results.append
            with pytest.raises(api.IZoneBusyError):
                await client.async_command({"SysOn": 1})
    finally:
        await mock.server.close()
    assert results == [False]


async def test_command_result_callback_reports_connection_failure(monkeypatch) -> None:
    monkeypatch.setattr(api, "CONNECT_RETRY_DELAYS", (0, 0))
    results: list[bool] = []
    async with ClientSession() as session:
        client = api.IZoneApi(session, "127.0.0.1:1")  # nothing listening
        client.on_command_result = results.append
        with pytest.raises(api.IZoneConnectionError):
            await client.async_command({"SysOn": 1})
    assert results == [False]


async def test_post_retries_transient_connection_failure(monkeypatch) -> None:
    monkeypatch.setattr(api, "CONNECT_RETRY_DELAYS", (0, 0))
    # Bridge drops the first request (503), then answers - the retry should
    # ride over it so a single blip doesn't fail the call.
    mock = MockBridge(command_fail_times=1)
    await mock.server.start_server()
    try:
        async with ClientSession() as session:
            client = api.IZoneApi(session, mock.host)
            await client.async_command({"SysOn": 1})  # must not raise
        assert mock.command_calls == 2  # one failure + one success
    finally:
        await mock.server.close()


async def test_connection_error_raises(monkeypatch) -> None:
    monkeypatch.setattr(api, "CONNECT_RETRY_DELAYS", (0, 0))
    async with ClientSession() as session:
        client = api.IZoneApi(session, "127.0.0.1:1")  # nothing listening
        with pytest.raises(api.IZoneConnectionError):
            await client.async_get_system()


def test_err_str_falls_back_to_type_name() -> None:
    # asyncio.TimeoutError stringifies to '' - the cause of the empty
    # "Error talking to iZone bridge:" log lines seen in the field.
    assert api._err_str(TimeoutError()) == "TimeoutError"
    assert api._err_str(ValueError("boom")) == "boom"
    assert api._err_str(None) == "unknown error"
