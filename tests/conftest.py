"""Shared pytest fixtures.

Uses pytest-homeassistant-custom-component for the HA-level tests. Those
modules declare `pytestmark = HA_FIXTURES` (recorder must start before
`hass`, and the integration depends on the recorder). Client tests run
against tests/fake_portal.py on a local aiohttp test server; nothing ever
talks to the real portal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.mainzer_netze.api import MainzerNetzeClient

from .fake_portal import PASSWORD, PREFIX, USERNAME, FakePortal, load_fixture


@pytest.fixture
def init_data() -> dict[str, Any]:
    return load_fixture("init_data.json")


@pytest.fixture
def tree() -> dict[str, Any]:
    return load_fixture("tree.json")


@pytest.fixture
def values_day() -> dict[str, Any]:
    return load_fixture("values_day.json")


@pytest.fixture
def values_dst() -> dict[str, Any]:
    return load_fixture("values_dst.json")


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
async def portal_server(portal: FakePortal, socket_enabled: None) -> AsyncIterator[TestServer]:
    # socket_enabled: the HA test plugin blocks sockets; the fake portal needs
    # a localhost socket (pytest-homeassistant still restricts it to 127.0.0.1).
    server = TestServer(portal.app(), host="127.0.0.1")
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
async def http_session() -> AsyncIterator[aiohttp.ClientSession]:
    # unsafe=True: aiohttp's jar ignores cookies for IP hosts like 127.0.0.1 otherwise
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        yield session


@pytest.fixture
def make_client(portal_server: TestServer, http_session: aiohttp.ClientSession):
    """Factory for a client pointed at the fake portal, with fast timings."""

    def factory(username: str = USERNAME, password: str = PASSWORD, **kwargs: Any) -> MainzerNetzeClient:
        kwargs.setdefault("min_request_interval", 0.0)
        kwargs.setdefault("retry_delays", (0.0, 0.0))
        return MainzerNetzeClient(
            http_session,
            username,
            password,
            base_url=str(portal_server.make_url(PREFIX)),
            **kwargs,
        )

    return factory
