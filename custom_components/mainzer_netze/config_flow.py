"""Config flow for the Mainzer Netze integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    MainzerNetzeAuthError,
    MainzerNetzeCaptchaRequiredError,
    MainzerNetzeClient,
    MainzerNetzeConnectionError,
    MainzerNetzeError,
)
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
STEP_REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})


class MainzerNetzeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handles setup and reauthentication via the UI."""

    VERSION = 1

    async def _async_validate(self, username: str, password: str) -> str | None:
        """Logs in once and checks that meters are visible. Returns an error key or None."""
        # Short-lived session, closed right after validation.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        client = MainzerNetzeClient(session, username, password)
        try:
            await client.async_login()
            meters = await client.async_get_meters()
        except MainzerNetzeCaptchaRequiredError:
            return "captcha_required"
        except MainzerNetzeAuthError:
            return "invalid_auth"
        except MainzerNetzeConnectionError:
            return "cannot_connect"
        except MainzerNetzeError:
            _LOGGER.exception("Unexpected response from the Mainzer Netze portal")
            return "unknown"
        finally:
            await client.async_logout()
            await session.close()
        return None if meters else "no_meters"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            error = await self._async_validate(username, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_create_entry(
                    title=username,
                    data={CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            # Keep the username on errors, never echo the password back.
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME)}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_validate(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
