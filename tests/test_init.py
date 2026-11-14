"""Tests for entry setup/unload, sensors and diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mainzer_netze.api import MainzerNetzeAuthError, MainzerNetzeConnectionError
from custom_components.mainzer_netze.const import DOMAIN
from custom_components.mainzer_netze.diagnostics import async_get_config_entry_diagnostics

from .fake_portal import FEATURE_ID, INDICATOR_ID, PASSWORD, USERNAME
from .helpers import ENTRY_DATA, HA_FIXTURES, LOCATION_NO, FakeClient, local

pytestmark = HA_FIXTURES


@pytest.fixture
def fake_client(freezer: FrozenDateTimeFactory) -> Iterator[FakeClient]:
    freezer.move_to(local(2026, 11, 13, 10))
    client = FakeClient()
    with patch("custom_components.mainzer_netze.MainzerNetzeClient", return_value=client):
        yield client


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=USERNAME, title=USERNAME)
    entry.add_to_hass(hass)
    return entry


def _entity_id(hass: HomeAssistant, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def test_setup_creates_sensors_and_unloads(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    last_day = hass.states.get(_entity_id(hass, f"{INDICATOR_ID}_{FEATURE_ID}_last_day"))
    assert last_day is not None
    assert float(last_day.state) == 24.0
    assert last_day.attributes["unit_of_measurement"] == "kWh"
    assert last_day.attributes["device_class"] == "energy"
    assert "state_class" not in last_day.attributes  # deliberately no HA-generated statistics
    assert last_day.attributes["date"] == "2026-11-12"
    assert last_day.attributes["statistic_id"].startswith("mainzer_netze:")

    data_until = hass.states.get(_entity_id(hass, f"{INDICATOR_ID}_data_until"))
    assert datetime.fromisoformat(data_until.state) == local(2026, 11, 13)
    last_import = hass.states.get(_entity_id(hass, f"{INDICATOR_ID}_last_import"))
    assert datetime.fromisoformat(last_import.state) == local(2026, 11, 13, 7)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_portal_unreachable(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.errors["async_get_meters"] = [MainzerNetzeConnectionError("down")]
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_error_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.errors["async_get_meters"] = [MainzerNetzeAuthError("bad")]
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_diagnostics_redacts_personal_data(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diagnostics)
    for secret in (USERNAME, PASSWORD, LOCATION_NO, LOCATION_NO.lower(), "Example Customer"):
        assert secret not in text
    assert diagnostics["last_update_success"] is True
    assert diagnostics["meters"][0]["features"][FEATURE_ID]["last_day"]["value"] == 24.0
