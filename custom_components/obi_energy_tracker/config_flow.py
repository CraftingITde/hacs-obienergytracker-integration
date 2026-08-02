"""Config flow for Obi EnergyTracker integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ObiAuthError, ObiConnectionError, ObiEnergyTrackerAPI
from .const import (
    CONF_BRIDGE_ID,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class ObiEnergyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Obi EnergyTracker."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> ObiEnergyTrackerOptionsFlow:
        """Get the options flow for this handler."""
        return ObiEnergyTrackerOptionsFlow()

    async def _async_validate(
        self, email: str, password: str, country: str
    ) -> tuple[dict[str, str] | None, str | None]:
        """Try to log in and resolve the bridge.

        Returns ``(ids, error_key)`` — exactly one of the two is set. Connection
        problems are reported separately from rejected credentials, so a broken
        network or a missing CA bundle no longer shows up as "invalid password".
        """
        session = async_create_clientsession(self.hass)
        api = ObiEnergyTrackerAPI(
            session, email=email, password=password, country=country
        )

        try:
            await api.async_login()
        except ObiAuthError as err:
            _LOGGER.debug("Authentication rejected: %s", err)
            return None, "invalid_auth"
        except ObiConnectionError as err:
            _LOGGER.debug("Could not reach the backend: %s", err)
            return None, "connection_error"

        if info := await api.async_get_bridge_info():
            return info, None
        return None, "no_devices"

    async def async_step_discovery(  # pylint: disable=unused-argument
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle discovery."""
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_configured()

            info, error = await self._async_validate(
                user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                user_input.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            )
            if info:
                user_input[CONF_BRIDGE_ID] = info["bridge_id"]
                user_input[CONF_DEVICE_ID] = info["device_id"]
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )
            errors["base"] = error or "connection_error"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication after the backend rejected the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password and verify it."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            info, error = await self._async_validate(
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
            )
            if info:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_BRIDGE_ID: info["bridge_id"],
                        CONF_DEVICE_ID: info["device_id"],
                    },
                )
            errors["base"] = error or "connection_error"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class ObiEnergyTrackerOptionsFlow(OptionsFlow):
    """Handle options flow for Obi EnergyTracker."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=current_scan_interval
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
