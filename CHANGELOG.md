# Changelog

Versions follow [semantic versioning](https://semver.org/). The version in
`manifest.json` is what HACS installs, and each release tag must match it. A
release's notes are its section of this file.

## 1.14.0

- **Reset the filter warning from Home Assistant.** A new button, **Reset
  filter warning** (`button.izone_<uid>_reset_filter_warning`, on the bridge
  device), clears the controller's filter reminder after you clean the filter.
  That restarts the reminder timer, the same as resetting it on the wall
  controller or in the iZone app. The "filter due" notification now points at
  the button, and still clears itself once the warning goes.
- The command, `{"WarningReset":"filter"}`, isn't in iZone's HTML reference.
  The header file calls it `ResetWarning` too, but on a real controller
  `ResetWarning` gets `{ERROR}` and does nothing. `WarningReset` works, but
  answers with an empty reply instead of `{OK}`. So the button ignores the
  reply and reads the system status back: if the warning is still on, the press
  fails with an error saying so. The command doesn't count against bridge
  health, so its odd reply can't trip "Bridge overloaded".
- This changelog, backfilled from the GitHub release notes, and a release
  workflow that publishes each version's section as its notes.

## 1.13.0 (2026-11-09)

**Climate control for constant zones.** iZone lets a constant zone also run to its own setpoint (`ConstClimate`). Until now such a zone only got a `switch`, so its setpoint couldn't be read or set from Home Assistant, and turning the switch on (open) took it out of climate control. These zones now get the same zone `climate` entity as temperature-controlled zones: off (close) / fan-only (open) / heat-cool (climate control) + setpoint. Thanks @JarredSteenvoorden (#22).

Constant zones, on either entity, carry the attributes `constant_zone` and `constant_active`; `constant_active` says whether the controller is holding the zone open right now to spill air.

#### Breaking

If you have a constant zone with climate control turned on, its `switch.<zone>` is replaced by `climate.<zone>_<zone>`. Delete the leftover switch (it shows as no longer provided) and point any automations at the climate entity. Other zones are unchanged.

## 1.12.0 (2026-10-21)

**Notifications in Home Assistant.** The integration now raises its own persistent notifications:

- **iZone: filter due for a clean**, when the controller's filter warning comes on. It's raised once per warning, so dismissing it won't bring it back every poll. It clears itself when the warning goes, after the reminder is reset on the wall controller or in the iZone app. It's only checked on real controller data, never on a zeroed reply.
- **iZone firmware updated**, when the firmware check (every 6 hours, since v1.10.0) finds a change. It lists what changed, e.g. `COCBH-4F5: 4.36 -> 4.39`.

**An options screen.** **Configure** on the integration has one switch, *Notifications in Home Assistant*, on by default. Turning it off dismisses any notifications already showing.

## 1.11.0 (2026-10-21)

**Zeroed bridge replies are ignored.** Now and then the bridge answers a read with every live field zeroed (on, mode, fan and temperatures all 0, zones with mode 0) while the unit is running. Home Assistant used to take that at face value and showed the unit and zones as off for about a minute. Mode and fan 0 are the spec's "not available" values, so those replies are now ignored and the last state kept. Nothing is re-sent because of them. If the replies stay zeroed for 5 minutes, the controller has probably stopped talking to the bridge, and the entities go unavailable.

**Fan speeds match the unit.** The AC entity offers only the speeds the unit reports through `FanAutoType`: low/medium/high/auto on a 3-speed fan, plus top on a 4-speed one. With fan auto disabled it offers low/medium/high.

## 1.10.0 (2026-10-21)

**Firmware sensors.** Each module the hub reports (the hub, the wall controller and each board) gets a diagnostic `Firmware <module>` sensor, e.g. `4.36`, with the part code as an attribute. The hub version also shows on the bridge's device page and in diagnostics. Versions are read every 6 hours using request Type 6, which isn't in the published spec. A failed read keeps the last versions and never fails the poll. Updates can still only be installed from the iZone app; these sensors show what's installed and when it changes.

**README.** The audit of the built-in integration is now a comparison against `python-izone` 1.3.11 (Home Assistant 2026.11), which speaks V2 too. The built-in integration is the supported path; this one is an experimental V2 playground.

**Removed the unsourced Quiet/Turbo fan values** (`SysFan` 6/7). Neither the spec header nor the Developer Portal defines them. They were never offered as fan modes, so nothing you see changes.

## 1.9.2 (2026-10-14)

**An icon and logo.** The integration now has its own icon, in HACS and on the Integrations page. It shows a fan hub (the bridge) feeding four zones: heating, on, closed and cooling. The logo has a dark-mode version.

Home Assistant 2026.3+ shows these from the integration's own `brand/` folder; this release already requires 2026.8+. No other changes since v1.9.1.

## 1.9.1 (2026-10-14)

**Fixes the `via_device` deprecation warning** that Home Assistant logs on startup ("calls `device_registry.async_get_or_create` with a deprecated `via_device` parameter ... This will stop working in Home Assistant 2027.8.0").

- Zone devices now link to the bridge using `via_device_id`, the bridge's device registry id. The bridge device is registered when the integration sets up, before any entities are added.
- Existing devices, areas and entity IDs are unchanged.

**Requires Home Assistant 2026.8.0 or newer** (was 2024.8.0), because older versions don't accept `via_device_id`. On an older Home Assistant, stay on v1.9.0.

## 1.9.0 (2026-10-14)

**Edit favourites from Home Assistant.** The new `izone_v2.update_favourite` action changes what a favourite stores: one zone's mode (open, close or auto) and setpoint, the fan, and/or the AC mode. It's saved on the controller, so the iZone app and wall controller see the change. Nothing is applied until the favourite is next activated.

- The controller's replies to favourite edits are unreliable. It answers `{ERROR}` to a command sent ~0.3 s after the last, and sometimes `{ERROR}` or nothing for edits it does keep. Edits are spaced 2 s apart, checked by reading the favourite back, and re-sent if they didn't stick. They don't count towards **Bridge overloaded**.
- Favourite scenes now show what they store: `zones`, `fan`, `mode` and `favourite_number` attributes.

**Diagnostics**

- **"Return air temperature" is now "Control temperature"** (same entity ID). On a zone-controlled system it's the temperature of the zone the unit is following, not the return air.
- New **Control zone** sensor: the zone the unit is following right now. The unit eases off as that zone nears its setpoint, even if other zones still want air.
- New **AC run hours** sensor, from the controller's run-hours counter.
- The filter warning shows the controller's reminder interval (`reminder_interval_months`).

## 1.8.0 (2026-09-28)

**Scenes are now followed through.** After a favourite scene, each zone's target is checked on every poll until the zone matches. Anything missing is re-sent (at most once a minute, up to 10 times).

- **Fixes closed zones opened by a scene keeping their old setpoint.** The iZone controller discards a setpoint sent to a zone that isn't under climate control and switches it to Auto at its *old* setpoint. The setpoint is now sent only after the zone reads back as Auto.
- **Zones with a dropped-out sensor are waited for, even for hours**, instead of being given up after 15 minutes. When the sensor recovers, the zone is corrected from the controller's remembered state to the scene's target.
- A target is dropped when a newer scene replaces it, when the zone is changed from HA, the iZone app or the wall controller, or after 12 hours.
- `climate.set_temperature` on a zone that's off now switches it to `heat_cool` first, and the setpoint follows once that lands.
- New diagnostic sensor **Pending zone changes** (count, with per-zone details in its attributes).

Targets are held in memory, so a Home Assistant restart clears them.

## 1.7.0 (2026-07-24)

### Climate: honour `hvac_mode` in `set_temperature`; clear errors for zone unit-modes

Prompted by a real automation failure — `climate.set_temperature` with `hvac_mode: heat` aimed at the AC unit **and** its zones failed with a cryptic *"Could not perform action for None"*, because a zone has no `heat` mode.

- **`hvac_mode` in `set_temperature` is now honoured** — the mode is applied before the setpoint (matching standard HA climate entities), instead of being silently ignored.
- **Unit-level modes on a zone now give a clear error.** A zone only opens / closes / climate-controls (it follows the unit); `heat`/`cool`/`dry` are whole-unit modes. Asking a zone for one raises a `ServiceValidationError` that names the problem, instead of a cryptic failure — both via `climate.set_hvac_mode` and via an `hvac_mode` bundled into `set_temperature`.
- **Docs:** README now explains heat/cool are unit-level, and how a blanket "heat the unit + its zones to 22°" should be structured (`heat` on the unit, `heat_cool` on the zones).

## 1.6.0 (2026-07-23)

### Apply scenes zone-by-zone when a zone sensor is faulted

When a favourite drives a **climate (Auto) zone whose sensor is faulted**, the controller refuses to apply the favourite as a whole — previously the *entire* scene silently failed, healthy zones included.

Now, when a faulted climate zone is detected, the scene is applied **zone by zone**:

- Healthy zones (and any open/close target, which needs no sensor) apply **immediately**.
- Faulted **Auto** zones are **deferred** and re-applied automatically once their sensor recovers (given up after 15 minutes).
- All-healthy scenes still use the fast single `FavouriteSet` path, unchanged.

Also: unconfirmed scenes now log **which zones didn't match and why** instead of a generic warning.

## 1.5.0 (2026-07-20)

### Added
- **Confirmed favourite scenes.** When you activate a favourite `scene`, the integration now reads the zones back, checks they match the favourite's stored config, and **re-applies a couple of times if it didn't take** — then logs a warning if it still can't confirm. A scene that quietly failed is now self-healing and visible instead of silent. (Zones with a faulty sensor, and controller-managed constant zones, are skipped so they don't cause false retries.)

### Changed
- **Commands are now paced** — a short gap is held between consecutive commands so a burst (e.g. an HA-native scene setting every zone at once) is spaced out instead of hammering the bridge/controller into a `{BUSY}` cascade. Reads aren't affected.

### Note on `{BUSY}`
That state often originates with the **wall controller** (the actual brain, which the bridge just relays) rather than the bridge itself — so a persistently busy system can point at the controller or a **faulty zone sensor** (a climate zone whose wireless sensor keeps dropping out). Check the per-zone *Sensor signal* / *Sensor fault* entities. To avoid the burst entirely, prefer these favourite scenes over HA-native scenes that set each zone individually.

## 1.4.0 (2026-07-20)

### Added — recovering a wedged bridge
The iZone bridge is a single-connection server; a burst of commands (e.g. an HA-native scene setting many zone climate entities at once) can leave it replying `{BUSY}` to everything until it catches up. Commands are retried, but if the bridge stays stuck they're eventually abandoned (and logged) rather than queued forever.

To make that recoverable via automation:
- **New `binary_sensor` "Bridge overloaded"** (diagnostic) — turns on when commands keep failing (≥3 within 5 minutes).
- **New `sensor` "Command failures"** (diagnostic) — running count over the last 5 minutes, for dashboards / numeric-threshold automations.

Drive a recovery automation off either — e.g. power-cycle the iZone hub via a smart plug when "Bridge overloaded" stays on. The README has a ready-to-adapt example.

### Changed
- More patience before a command is given up on (busy-retry budget 5 → 6, with a longer backoff tail).
- Diagnostics now include the bridge-overload health fields.

### Tip
Prefer this integration's own **favourite `scene` entities** (a single `FavouriteSet` the bridge applies internally) over HA-native scenes that set every zone individually — that avoids the command burst that overloads the bridge in the first place.

## 1.3.1 (2026-07-19)

### Fixed
- **Scenes (and other entities) no longer drop out when the bridge is briefly unresponsive.** The iZone bridge is a slow single-connection server that occasionally times out a request; previously a single timed-out poll marked the whole integration as failed, made every entity — including scenes — `unavailable`, and silently dropped `scene.turn_on` calls (logged as *"entities are missing or not currently available"*). Reads and commands are now retried a couple of times before being reported as a failure, and **scene entities stay triggerable regardless of poll state**.
- Connection-error log messages are no longer empty (timeouts previously logged `Error talking to iZone bridge at …:` with nothing after the colon).

### Notes
- Commands now debug-log the exact payload and the bridge's raw reply (enable `custom_components.izone_v2: debug`) — handy for diagnosing any future "command accepted but nothing happened" reports.
- "All scenes show *Activated* in HA" is expected Home Assistant behaviour — scenes are stateless and always display their last-activated time; it isn't something the integration controls.

## 1.3.0 (2026-07-08)

### Added
- **Sensor signal strength**: wireless zone sensors now expose a "Sensor signal" sensor (full/half/quarter/none) alongside the existing battery level — useful for catching a sensor drifting toward drop-out before it actually goes `unknown`.

### Changed
- The per-zone **damper fault** and **sensor fault** binary sensors are now enabled by default (previously hidden diagnostic entities). These are the cleanest entities to build "sensor dropped out" automations on — no need to dig into Settings to enable them.

Neither change adds any extra requests to the bridge: both fields already ride along in the per-zone data fetched on every 30s poll.

## 1.2.0 (2026-07-08)

### Added
- **Scenes**: iZone "favourites" (saved AC mode/fan/setpoint + per-zone presets you can apply on demand from the iZone app) are now exposed as Home Assistant `scene` entities — trigger them from automations, the dashboard, or `scene.turn_on`. Up to 9 favourites are supported; only named/configured slots are exposed. Favourites are discovered once when the integration starts (they rarely change) — reload the integration after adding or renaming one in the iZone app to pick up the change.
- Diagnostics download now includes favourite state.

## 1.1.1 (2026-07-08)

### Fixed
- Commands no longer fail outright when the bridge is mid-actuation on a previous command: it replies `{BUSY}` (e.g. while a damper motor is still moving) rather than accepting queued work. This surfaced as scene application errors like:
  ```
  Bridge ... rejected command {'ZoneMode': {'Index': 1, 'Mode': 3}}: '{BUSY}'
  ```
  `async_command` now retries on `{BUSY}` up to 5 times with backoff (0.5/1/2/4/6s) before giving up, while still failing immediately on real rejections (`InvalidRequest`, `InvalidUser`, `UserNotAllowed`, `Error`).

## 1.1.0 (2026-07-07)

### Fixed
- Commands no longer report failure on success: real bridges reply `{OK}` (v1-style bracing) rather than the documented bare `OK`. This fixes errors like `Bridge ... rejected command {'SysFan': 4}: '{OK}'` when setting fan mode, HVAC mode, etc.

### Changed
- Each zone is now its own Home Assistant device, named after the zone, with the zone name as suggested area — zone entities can be assigned to areas/rooms directly, no need to untick "Use device area". After upgrading, existing zone entities keep their history/entity IDs but move to the new zone devices.

### Added
- Sensors: supply air temp, return air temp, per-zone temperature, damper position, wireless sensor battery, AC error code, sleep-timer remaining
- Binary sensors: filter warning, per-zone damper fault and sensor fault (faults disabled by default)
- Numbers: sleep timer (0–120 min), per-zone min/max airflow (disabled by default)
- Switch: iSave (when supported by the system)
- Diagnostics download for issue reports
- GitHub Actions CI (hassfest, HACS validation, ruff, mock-bridge protocol tests)

