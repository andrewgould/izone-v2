"""Config flow for the iZone V2 integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DiscoveredBridge, IZoneApi, IZoneError, async_discover
from .const import CONF_HOST, CONF_UID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_MANUAL_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


class IZoneV2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery-first setup with a manual-IP fallback."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredBridge] = {}

    async def _async_validate(self, host: str) -> str:
        """Query the bridge and return its unique system ID."""
        api = IZoneApi(async_get_clientsession(self.hass), host)
        response = await api.async_get_system()
        return str(response["AirStreamDeviceUId"])

    async def _async_create(self, host: str) -> ConfigFlowResult:
        uid = await self._async_validate(host)
        await self.async_set_unique_id(uid)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return self.async_create_entry(
            title=f"iZone {uid}", data={CONF_HOST: host, CONF_UID: uid}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover bridges on the local network."""
        try:
            bridges = await async_discover()
        except IZoneError:
            bridges = []

        self._discovered = {
            b.uid: b
            for b in bridges
            if b.supports_v2
            and b.uid not in self._async_current_ids(include_ignore=False)
        }
        v1_only = [b for b in bridges if not b.supports_v2]
        if v1_only:
            _LOGGER.warning(
                "Found iZone bridge(s) that only support the legacy v1 API "
                "(no 'iZoneV2' in discovery response): %s. Use the built-in "
                "izone integration for these",
                ", ".join(f"{b.uid}@{b.host}" for b in v1_only),
            )

        if not self._discovered:
            return await self.async_step_manual()
        return await self.async_step_pick()

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the discovered bridges (or enter an IP manually)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            uid = user_input[CONF_UID]
            if uid == "manual":
                return await self.async_step_manual()
            try:
                return await self._async_create(self._discovered[uid].host)
            except IZoneError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - show an error, not a 500
                _LOGGER.exception("Unexpected error validating iZone bridge")
                errors["base"] = "cannot_connect"

        options = {
            uid: f"iZone {uid} ({bridge.host})"
            for uid, bridge in self._discovered.items()
        }
        options["manual"] = "Enter IP address manually"
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required(CONF_UID): vol.In(options)}),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual IP address entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_create(user_input[CONF_HOST])
            except IZoneError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - show an error, not a 500
                _LOGGER.exception("Unexpected error validating iZone bridge")
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="manual", data_schema=STEP_MANUAL_SCHEMA, errors=errors
        )
