"""Constants for the iZone V2 integration."""

from __future__ import annotations

DOMAIN = "izone_v2"

CONF_HOST = "host"
CONF_UID = "uid"

MANUFACTURER = "iZone (Airstream Components)"

# Seconds between polls of the bridge. The bridge also broadcasts change
# notifications on UDP 7005 which trigger an immediate refresh, so this is
# just a safety net.
POLL_INTERVAL = 30

# Bridge-overload signal: a rolling window (seconds) over which failed
# commands are counted, and the count at which the "bridge overloaded"
# binary sensor turns on. Tuned so brief scene-storm contention (a few
# failures that quickly recover) doesn't trip it, but a genuinely wedged
# hub does - letting an automation power-cycle the hardware.
COMMAND_FAILURE_WINDOW = 300
OVERLOAD_THRESHOLD = 3
