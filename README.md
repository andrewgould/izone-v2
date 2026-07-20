# iZone V2 — Home Assistant custom integration

[![CI](https://github.com/andrewgould/izone-v2/actions/workflows/ci.yml/badge.svg)](https://github.com/andrewgould/izone-v2/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=andrewgould&repository=izone-v2&category=integration)

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

**HACS:** click the "Open in HACS" badge above to open this repository directly
in your own Home Assistant instance (requires the My Home Assistant integration,
which is on by default), or add it manually as a custom repository
(type: Integration). Then install "iZone V2 (Local API)" and restart.

Then: *Settings → Devices & Services → Add Integration → iZone V2 (Local API)*.
Bridges are discovered automatically (UDP broadcast); you can also enter the
IP address manually.

## What you get

| iZone concept | Home Assistant entity |
|---|---|
| AC unit | `climate` — off/cool/heat/heat-cool/fan-only/dry, fan low/med/high/auto/top, setpoint (0.5 °C steps, Eco-lock limits respected) |
| Temperature-controlled zones | `climate` per zone — off (close) / fan-only (open) / heat-cool (climate control) + zone setpoint |
| Open/close & constant zones | `switch` per zone (on = damper open) |
| iSave | `switch` (only if the system supports it) |
| Supply / return air temperature | `sensor` |
| Zone temperature | `sensor` per zone with a sensor |
| Damper position | `sensor` per zone (diagnostic) |
| Wireless sensor battery | `sensor` (full/half/empty, diagnostic) |
| Wireless sensor signal strength | `sensor` (full/half/quarter/none, diagnostic) |
| Filter warning, damper fault, sensor fault | `binary_sensor` (diagnostic) |
| Bridge overloaded | `binary_sensor` (diagnostic) — on when commands are repeatedly failing |
| Command failures (recent) | `sensor` (diagnostic) — count of failed commands in the last 5 min |
| Sleep timer | `number` (0–120 min in 30-min steps) |
| Zone min/max airflow | `number` per zone (config; disabled by default) |
| Favourites | `scene` per configured favourite (up to 9) |

Wireless zone sensors (battery-powered) also expose signal strength and battery
level, so a sensor drifting toward "none"/"empty" can be caught before it drops
out entirely (shows up as the zone temperature going `unknown`). The damper/sensor
fault binary sensors are enabled by default for the same reason — none of this
costs extra requests to the bridge, since it all rides along in the zone data
already being polled every 30 s.

Each zone is its own Home Assistant **device**, named after the zone and with the
zone name as its *suggested area* — so entities land in the right room by default
and can be re-assigned per zone. Diagnostics download is supported for issue
reports (Settings → Devices → iZone bridge → Download diagnostics).

### Recovering a wedged bridge

The iZone bridge is a single-connection embedded server. If it's hit with a
burst of commands — e.g. a Home Assistant scene that sets many zone climate
entities at once — it can fall behind and reply `{BUSY}` to everything until it
catches up. Commands are retried with backoff, but if the bridge stays busy the
command is eventually abandoned (and logged) rather than queued forever.

The **Bridge overloaded** binary sensor turns on when commands keep failing
(≥3 failures within 5 minutes), and the **Command failures** sensor exposes the
running count. Use either to drive a recovery automation — for example, power-
cycling the hub via a smart plug:

```yaml
automation:
  - alias: Power-cycle iZone hub when overloaded
    triggers:
      - trigger: state
        entity_id: binary_sensor.izone_XXXXXXXXX_bridge_overloaded
        to: "on"
        for: "00:02:00"
    actions:
      - action: switch.turn_off
        target: { entity_id: switch.izone_hub_power }
      - delay: "00:00:20"
      - action: switch.turn_on
        target: { entity_id: switch.izone_hub_power }
```

To avoid triggering the overload in the first place, prefer this integration's
own **favourite `scene` entities** (a single `FavouriteSet` command the bridge
applies internally) over HA-native scenes that set every zone individually.

**Favourites** (saved AC mode/fan/setpoint + per-zone presets in the iZone app)
show up as `scene` entities, named after the favourite. Only slots with a name
set are exposed — empty slots are skipped. Favourites are discovered once when
the integration starts, not on every poll, since they rarely change; reload the
integration (Settings → Devices & Services → iZone V2 → ⋮ → Reload) after
adding or renaming a favourite in the iZone app to pick up the change.

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
| Favourite *n* state | `POST /iZoneRequestV2` `{"iZoneV2Request":{"Type":3,"No":n,"No1":0}}` |
| Trigger favourite *n* | `POST /iZoneCommandV2` `{"FavouriteSet":n+1}` (1-9) |

Commands normally return the literal string `OK` on success. Real bridges also
reply with v1-style bracing (`{OK}`) and, while still actuating a previous
command (e.g. a damper motor in transit), `{BUSY}` — which this integration
retries automatically rather than treating as a failure.

The bridge is a single-connection embedded server and occasionally drops or
times out a request (especially just after a command, while it's actuating).
Both reads and commands are retried a couple of times before being reported as
a failure, so a single dropped request doesn't knock every entity offline.
Scene entities stay available regardless of poll state, since triggering a
favourite doesn't depend on fresh data.
