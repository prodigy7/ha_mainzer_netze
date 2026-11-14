"""Mainzer Netze integration for Home Assistant.

Imports quarter-hour consumption data from the Mainzer Netze metering portal
(msb.mainzer-netze.de) as long-term statistics for the Energy dashboard.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import MainzerNetzeClient
from .const import CONF_PASSWORD, CONF_USERNAME
from .coordinator import MainzerNetzeConfigEntry, MainzerNetzeCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: MainzerNetzeConfigEntry) -> bool:
    """Sets up a config entry."""
    # Dedicated session: the portal authenticates via a session cookie, which
    # must not leak into HA's shared session. Closed automatically on unload.
    client = MainzerNetzeClient(
        async_create_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = MainzerNetzeCoordinator(hass, entry, client)
    # Raises ConfigEntryNotReady / ConfigEntryAuthFailed as appropriate.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MainzerNetzeConfigEntry) -> bool:
    """Unloads a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
