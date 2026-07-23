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

# When a scene touches a climate zone whose sensor is faulted, the controller
# won't apply the favourite as a unit, so we apply it zone-by-zone and defer
# the faulted climate zones - re-applying each one when its sensor recovers.
# A deferred target is dropped after this long so a sensor that only recovers
# much later doesn't snap a stale scene target into place unexpectedly.
SCENE_DEFER_EXPIRY = 900  # seconds (15 min)
