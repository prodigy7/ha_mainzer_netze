"""Diagnostics support for the Mainzer Netze integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_USERNAME
from .coordinator import MainzerNetzeConfigEntry

# Personal data: credentials, the customer's name and the metering point id.
TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "name", "location_no", "statistic_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MainzerNetzeConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    meters = [asdict(meter_state) for meter_state in (coordinator.data or {}).values()]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "meters": async_redact_data(meters, TO_REDACT),
    }
