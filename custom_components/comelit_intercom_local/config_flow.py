"""Config flow for Comelit Local integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_TOKEN

from .auth import authenticate
from .client import IconaBridgeClient
from .const import CONF_ENABLE_NOTIFICATIONS, CONF_HTTP_PORT, DEFAULT_HTTP_PORT, DEFAULT_PORT, DOMAIN
from .exceptions import (
    AuthenticationError,
    ConnectionComelitError as ComelitConnectionError,
)
from .token import extract_token

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional("name", default=""): str,
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): int,
        vol.Optional(CONF_TOKEN, default=""): str,
        vol.Optional(CONF_PASSWORD, default="comelit"): str,
    }
)


class ComelitLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comelit Local."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return ComelitLocalOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get("name", "").strip()
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            http_port = user_input.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
            token = user_input.get(CONF_TOKEN, "").strip()
            password = user_input.get(CONF_PASSWORD, "comelit")
            title = name if name else f"Comelit {host}"

            # Auto-extract token if not provided
            if not token:
                try:
                    token = await extract_token(host, password, http_port)
                except Exception as err:
                    _LOGGER.exception("Token extraction failed: %s", err)  # nosemgrep: python-logger-credential-disclosure
                    errors["base"] = "token_extraction_failed"

            if not errors:
                client = IconaBridgeClient(host, port)
                try:
                    await asyncio.wait_for(client.connect(), timeout=10)
                    await asyncio.wait_for(authenticate(client, token), timeout=10)
                except AuthenticationError:
                    _LOGGER.warning("Authentication failed for %s", host)
                    errors["base"] = "invalid_auth"
                except (TimeoutError, ComelitConnectionError, OSError) as err:
                    _LOGGER.warning("Connection failed for %s: %s", host, err)
                    errors["base"] = "cannot_connect"
                finally:
                    await client.disconnect()

            if not errors:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_TOKEN: token,
                        CONF_HTTP_PORT: http_port,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class ComelitLocalOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Comelit Local (e.g. enable/disable notifications)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENABLE_NOTIFICATIONS, default=current): bool}
            ),
        )
