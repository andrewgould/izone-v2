"""Constants for the iZone V2 integration."""

from __future__ import annotations

DOMAIN = "izone_v2"

CONF_HOST = "host"
CONF_UID = "uid"

MANUFACTURER = "iZone (Airstream Components)"

# Seconds between polls of the bridge - the fallback source of truth. The
# bridge's UDP 7005 broadcasts also nudge a refresh (change-driven, ~2-3s after
# a real change), but polling is what guarantees state stays current if one is
# missed.
POLL_INTERVAL = 30
# Firmware only changes when someone updates it from the iZone app, so read it
# rarely: the bridge is a slow single-connection server.
FIRMWARE_INTERVAL = 6 * 3600  # seconds
FIRMWARE_RETRY = 600  # seconds, after a failed read

# Bridge-overload signal: a rolling window (seconds) over which failed
# commands are counted, and the count at which the "bridge overloaded"
# binary sensor turns on. Tuned so brief scene-storm contention (a few
# failures that quickly recover) doesn't trip it, but a genuinely wedged
# hub does - letting an automation power-cycle the hardware.
COMMAND_FAILURE_WINDOW = 300
OVERLOAD_THRESHOLD = 3

# After triggering a favourite ("scene"), how many times to re-apply if the
# zones don't match the favourite's stored config, and how long to wait for
# the controller to settle before reading back.
SCENE_VERIFY_RETRIES = 2
SCENE_VERIFY_DELAY = 2.0

# Editing a favourite (izone_v2.update_favourite): the controller answers
# {ERROR} to a favourite command that arrives ~0.3s after the previous one, and
# keeps it with a 1-2s gap (measured 15 Oct 2026). Each favourite command is
# followed by this pause, and whatever the read-back shows didn't stick is
# re-sent, for up to FAVOURITE_EDIT_ATTEMPTS rounds.
FAVOURITE_WRITE_SETTLE = 2.0
FAVOURITE_EDIT_ATTEMPTS = 3

# Zone targets set by a scene (or a setpoint that has to wait for its zone to
# switch to climate control) are kept and checked on every poll until the zone
# matches. Whatever is still missing is re-sent at most once per
# ZONE_TARGET_RESEND - the controller often needs well over a few seconds - and
# at most ZONE_TARGET_MAX_SENDS times. A zone whose sensor has dropped out is
# waited for without sending anything. A target is dropped when a newer scene
# replaces it, when the zone is changed some other way, or after
# ZONE_TARGET_EXPIRY - long enough to carry an evening scene through a night
# of sensor dropouts.
ZONE_TARGET_RESEND = 60  # seconds
ZONE_TARGET_MAX_SENDS = 10
ZONE_TARGET_EXPIRY = 12 * 3600  # seconds
