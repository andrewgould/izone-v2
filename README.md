# iZone V2 — Home Assistant custom integration

A replacement for Home Assistant's built-in [`izone`](https://www.home-assistant.io/integrations/izone/)
integration that speaks the **current, officially documented iZone local API ("V2")**
from the [iZone Developer Portal](https://developer.izone.com.au/), instead of the
legacy v1 protocol.

## Why this exists — audit of the built-in integration

iZone support stated that the Home Assistant integration's "commands to the iZone
Hub are not correct". An audit of the built-in integration (which delegates all
protocol work to the [`python-izone` / `pizone`](https://github.com/Swamp-Ig/pizone)
library) against the Developer Portal documentation found:

### What the built-in integration gets right

- **Discovery** — UDP broadcast `IASD` to `255.255.255.255:12107` matches the
  current official documentation exactly.
- **Change notifications** — listening on UDP 7005 for `iZoneChanged_*` matches
  the official v1 spec.
- **v1 commands** — every command `pizone` sends (`POST /SystemON`,
  `/SystemMODE`, `/SystemFAN`, `/UnitSetpoint`, `/ZoneCommand`,
  `/AirMinCommand`, `/AirMaxCommand`, `/SleepTimer`, `/FreeAir`) matches the
  official *v1* spec PDF — which is byte-identical to the
  `iZoneEthernetInterface.v1.pdf` still hosted on the Developer Portal today.

So "the commands are not correct" is not accurate against iZone's own v1 spec.

### What is genuinely wrong / outdated

1. **It only ever speaks the legacy v1 API**, even to bridges that advertise
   `iZoneV2` in their discovery response. iZone's currently documented and
   supported local API is V2 (`POST /iZoneRequestV2` and `POST /iZoneCommandV2`
   with integer enums and temperatures ×100) — a completely different command
   dialect. Newer hubs/firmware are developed and tested against V2 only;
   v1 handling on new firmware is exactly where "incorrect commands" symptoms
   come from.
2. **Out-of-spec fan value** — `pizone` sends `{"SystemFAN": "top"}`, but the v1
   spec only defines `low`/`medium`/`high`/`auto`. "Top" exists only in the V2
   API (`SysFan` = 5).
3. **Hand-rolled HTTP POST with no `Content-Type` header** —
   `pizone` bypasses aiohttp and writes a raw request containing only `Host`
   and `Content-Length`. iZone's official examples send
   `Content-Type: application/json`; strict embedded parsers can reject the
   former.
4. **12-zone ceiling** — v1 status endpoints only cover `Zones1_4/5_8/9_12`
   (`assert group in [0, 4, 8]`); V2 systems support 14 zones.
5. **Missing V2 state** — v1 cannot express V2 zone modes *Override*/*Constant*,
   fan speeds *Quiet*/*Turbo*, or Coolbreeze modes *Exhaust*/*PumpOnly*, so
   state from newer systems can be misread.

This integration implements the V2 API as documented (verified against
`iZone_JSON_datastrings.h` v1.41 and iZone's official Postman collection).

## Requirements

Your bridge must support the V2 API — its discovery response includes
`iZoneV2`. Almost every hub shipped since ~2017 does. If your bridge responds
with only `iZone`, keep using the built-in integration (v1 is all it speaks).

## Installation

**Manual:** copy `custom_components/izone_v2/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

**HACS:** add this repository as a custom repository (type: Integration),
install "iZone V2 (Local API)", restart.

Then: *Settings → Devices & Services → Add Integration → iZone V2 (Local API)*.
Bridges are discovered automatically (UDP broadcast); you can also enter the
IP address manually.

## What you get

| iZone concept | Home Assistant entity |
|---|---|
| AC unit | `climate` — off/cool/heat/heat-cool/fan-only/dry, fan low/med/high/auto/top, setpoint (0.5 °C steps, Eco-lock limits respected) |
| Temperature-controlled zones (type *Climate*) | `climate` per zone — off (close) / fan-only (open) / heat-cool (climate control) + zone setpoint |
| Open/close & constant zones | `switch` per zone (on = damper open) |

State refreshes every 30 s and immediately on the bridge's `iZoneChanged_*`
UDP broadcasts. All requests are serialised — the bridge's embedded HTTP
server can't handle concurrent requests.

## Protocol summary (V2)

| Action | Request |
|---|---|
| System state | `POST /iZoneRequestV2` `{"iZoneV2Request":{"Type":1,"No":0,"No1":0}}` |
| Zone *n* state | `POST /iZoneRequestV2` `{"iZoneV2Request":{"Type":2,"No":n,"No1":0}}` |
| System on/off | `POST /iZoneCommandV2` `{"SysOn":0\|1}` |
| Mode | `{"SysMode":1..5}` (cool, heat, vent, dry, auto) |
| Fan | `{"SysFan":1..5}` (low, med, high, auto, top) |
| Unit setpoint | `{"SysSetpoint":2250}` (°C ×100) |
| Zone mode | `{"ZoneMode":{"Index":n,"Mode":1\|2\|3}}` (open, close, climate) |
| Zone setpoint | `{"ZoneSetpoint":{"Index":n,"Setpoint":2250}}` |

All commands return the literal string `OK` on success.
