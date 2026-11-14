"""Tests for the config flow (setup + reauth)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mainzer_netze.api import (
    MainzerNetzeApiError,
    MainzerNetzeAuthError,
    MainzerNetzeCaptchaRequiredError,
    MainzerNetzeConnectionError,
)
from custom_components.mainzer_netze.const import CONF_PASSWORD, CONF_USERNAME, DOMAIN

from .fake_portal import PASSWORD, USERNAME
from .helpers import ENTRY_DATA, HA_FIXTURES, FakeClient

pytestmark = HA_FIXTURES


@pytest.fixture
def fake_client() -> Iterator[FakeClient]:
    client = FakeClient()
    with (
        patch(
            "custom_components.mainzer_netze.config_flow.MainzerNetzeClient",
            return_value=client,
        ),
        patch("custom_components.mainzer_netze.async_setup_entry", return_value=True),
    ):
        yield client


async def _start_user_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_user_flow_success(hass: HomeAssistant, fake_client: FakeClient) -> None:
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: f"  {USERNAME.upper()} ", CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME.upper()
    assert result["data"] == {CONF_USERNAME: USERNAME.upper(), CONF_PASSWORD: PASSWORD}
    assert result["result"].unique_id == USERNAME  # case-insensitive
    assert fake_client.logout_calls == 1  # validation session is closed


@pytest.mark.parametrize(
    ("method", "error", "expected"),
    [
        ("async_login", MainzerNetzeAuthError("bad"), "invalid_auth"),
        ("async_login", MainzerNetzeCaptchaRequiredError("captcha"), "captcha_required"),
        ("async_login", MainzerNetzeConnectionError("down"), "cannot_connect"),
        ("async_get_meters", MainzerNetzeApiError("weird"), "unknown"),
    ],
)
async def test_user_flow_errors_and_recovery(
    hass: HomeAssistant, fake_client: FakeClient, method: str, error: Exception, expected: str
) -> None:
    fake_client.errors[method] = [error]
    flow_id = await _start_user_flow(hass)
    user_input = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}

    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_no_meters(hass: HomeAssistant, fake_client: FakeClient) -> None:
    fake_client.meters = []
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "no_meters"}


async def test_user_flow_already_configured(hass: HomeAssistant, fake_client: FakeClient) -> None:
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=USERNAME).add_to_hass(hass)
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert fake_client.calls == []  # aborted before contacting the portal


async def test_reauth_flow(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=USERNAME)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    fake_client.errors["async_login"] = [MainzerNetzeAuthError("still bad")]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-pass"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-pass"
    assert entry.data[CONF_USERNAME] == USERNAME
