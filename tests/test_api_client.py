"""Tests for MainzerNetzeClient against the in-process fake portal."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestServer

from custom_components.mainzer_netze.api import (
    MainzerNetzeApiError,
    MainzerNetzeAuthError,
    MainzerNetzeCaptchaRequiredError,
    MainzerNetzeClient,
    MainzerNetzeConnectionError,
)
from custom_components.mainzer_netze.const import PORTAL_TZ

from .fake_portal import FEATURE_ID, INDICATOR_ID, QUARTER_HOUR_VALUE, FakePortal


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=PORTAL_TZ)


# --- login ---


async def test_login_and_get_meters(make_client, portal: FakePortal) -> None:
    client: MainzerNetzeClient = make_client()
    meters = await client.async_get_meters()  # logs in implicitly
    assert client.logged_in
    assert [m.indicator_id for m in meters] == [INDICATOR_ID]
    assert portal.calls["/loginProcess"] == 1


async def test_wrong_password_raises_auth_error_without_retrying(make_client, portal: FakePortal) -> None:
    client = make_client(password="wrong")
    with pytest.raises(MainzerNetzeAuthError, match="Login failed"):
        await client.async_login()
    # Never hammer the login with bad credentials (captcha risk).
    assert portal.calls["/loginWidget.json"] == 1
    assert portal.calls["/loginProcess"] == 0
    assert not client.logged_in


async def test_wrong_password_on_data_call_is_auth_error(make_client, portal: FakePortal) -> None:
    with pytest.raises(MainzerNetzeAuthError):
        await make_client(password="wrong").async_get_meters()
    assert portal.calls["/loginWidget.json"] == 1


async def test_captcha_required(make_client, portal: FakePortal) -> None:
    portal.captcha_required = True
    with pytest.raises(MainzerNetzeCaptchaRequiredError):
        await make_client().async_login()
    assert portal.calls["/loginWidget.json"] == 1


async def test_login_process_redirect_to_start_is_auth_error(make_client, portal: FakePortal) -> None:
    # Validation passes, but the actual login bounces back to the login page.
    portal.force("/loginProcess", 200, '<form id="loginProcessForm"></form>')
    with pytest.raises(MainzerNetzeAuthError):
        await make_client().async_login()


async def test_login_validation_non_json_error_is_api_error(make_client, portal: FakePortal) -> None:
    portal.force("/loginWidget.json", 400, "<html>bad</html>")
    with pytest.raises(MainzerNetzeApiError):
        await make_client().async_login()


# --- session handling ---


async def test_expired_session_relogs_transparently(make_client, portal: FakePortal) -> None:
    client = make_client()
    await client.async_login()
    portal.expire_all_sessions()  # initData now answers HTTP 200 "{}"
    details = await client.async_get_meter_details(INDICATOR_ID)
    assert FEATURE_ID in details.features
    assert portal.calls["/loginProcess"] == 2
    assert portal.calls["/analysis/initData"] == 2


async def test_expired_session_on_tree_http_311(make_client, portal: FakePortal) -> None:
    client = make_client()
    await client.async_login()
    portal.expire_all_sessions()
    assert len(await client.async_get_meters()) == 1
    assert portal.calls["/indicatorGroupList.json"] == 2


async def test_html_instead_of_json_triggers_relogin(make_client, portal: FakePortal) -> None:
    client = make_client()
    await client.async_login()
    portal.force("/analysis/initData", 200, "<html>login page</html>")
    await client.async_get_meter_details(INDICATOR_ID)
    assert portal.calls["/loginProcess"] == 2


async def test_persistently_invalid_response_is_api_error_after_one_relogin(
    make_client, portal: FakePortal
) -> None:
    client = make_client()
    for _ in range(5):
        portal.force("/analysis/initData", 200, "{}", "application/json")
    with pytest.raises(MainzerNetzeApiError, match="even after re-login"):
        await client.async_get_meter_details(INDICATOR_ID)
    assert portal.calls["/analysis/initData"] == 2  # no endless loop
    assert portal.calls["/loginProcess"] == 2


async def test_logout_ends_session(make_client, portal: FakePortal) -> None:
    client = make_client()
    await client.async_login()
    await client.async_logout()
    assert not client.logged_in
    assert portal.calls["/logout"] == 1
    assert not any(portal.sessions.values())


async def test_logout_without_login_sends_nothing(make_client, portal: FakePortal) -> None:
    await make_client().async_logout()
    assert portal.calls["/logout"] == 0


async def test_logout_errors_are_swallowed(make_client, portal: FakePortal) -> None:
    client = make_client(retry_delays=())
    await client.async_login()
    portal.force("/logout", 503)
    await client.async_logout()  # must not raise
    assert not client.logged_in


# --- transient errors ---


async def test_transient_503_is_retried(make_client, portal: FakePortal) -> None:
    client = make_client()
    portal.force("/indicatorGroupList.json", 503)
    assert len(await client.async_get_meters()) == 1
    assert portal.calls["/indicatorGroupList.json"] == 2


async def test_persistent_503_raises_connection_error(make_client, portal: FakePortal) -> None:
    client = make_client(retry_delays=(0.0, 0.0))
    for _ in range(3):
        portal.force("/indicatorGroupList.json", 503)
    with pytest.raises(MainzerNetzeConnectionError):
        await client.async_get_meters()
    assert portal.calls["/indicatorGroupList.json"] == 3  # 1 try + 2 retries


async def test_value_request_503_on_lost_session_recovers(make_client, portal: FakePortal, init_data) -> None:
    # Live-verified: valueRequest without session answers 503 (not 311/{}).
    client = make_client()
    details = await client.async_get_meter_details(INDICATOR_ID)
    portal.expire_all_sessions()
    readings = await client.async_get_readings(
        details, FEATURE_ID, _local(2026, 11, 12), _local(2026, 11, 13)
    )
    assert len(readings) == 96
    assert portal.calls["/loginProcess"] == 2


async def test_timeout_raises_connection_error(make_client, portal: FakePortal) -> None:
    portal.delay_seconds = 0.5
    client = make_client(timeout=0.1, retry_delays=())
    with pytest.raises(MainzerNetzeConnectionError):
        await client.async_login()


async def test_unreachable_server_raises_connection_error(make_client, portal_server: TestServer) -> None:
    client = make_client(retry_delays=())
    await portal_server.close()
    with pytest.raises(MainzerNetzeConnectionError):
        await client.async_login()


# --- data ---


async def test_get_readings_chunks_requests(make_client, portal: FakePortal) -> None:
    client = make_client()
    details = await client.async_get_meter_details(INDICATOR_ID)
    start, end = _local(2026, 10, 1), _local(2026, 11, 13)  # 43 days, spans DST
    readings = await client.async_get_readings(details, FEATURE_ID, start, end)

    assert len(portal.value_ranges) == 4  # 14-day chunks
    assert portal.value_ranges[0][0] == start and portal.value_ranges[-1][1] == end
    # absolute hours incl. the extra DST hour (UTC: same-ZoneInfo subtraction is wall-clock)
    hours = (end.astimezone(UTC) - start.astimezone(UTC)) / timedelta(hours=1)
    assert hours == 43 * 24 + 1
    assert len(readings) == hours * 4
    assert readings[0].start == start and readings[-1].end == end
    assert all(prev.end == cur.start for prev, cur in itertools.pairwise(readings))
    assert sum(r.value for r in readings) == pytest.approx(hours * 4 * QUARTER_HOUR_VALUE)


async def test_requests_are_throttled(make_client, portal: FakePortal) -> None:
    client = make_client(min_request_interval=0.05)
    await client.async_get_meters()  # start, loginWidget, loginProcess(+redirect), tree
    gaps = [b - a for a, b in itertools.pairwise(portal.request_times)]
    # The redirect target (/home) is followed by aiohttp within one request.
    assert len(portal.request_times) >= 4
    assert sum(1 for gap in gaps if gap >= 0.045) >= 3
