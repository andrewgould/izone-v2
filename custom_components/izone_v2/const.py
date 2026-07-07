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
