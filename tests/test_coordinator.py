"""Tests for the coordinator: fetch window logic and full update cycles with the recorder."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

# Registers the config flow handler; HA only starts reauth flows for
# integrations whose handler is registered (done on setup in real HA).
from custom_components.mainzer_netze import config_flow  # noqa: F401
from custom_components.mainzer_netze.api import (
    Feature,
    MainzerNetzeApiError,
    MainzerNetzeAuthError,
    MainzerNetzeConnectionError,
)
from custom_components.mainzer_netze.const import DOMAIN, PORTAL_TZ
from custom_components.mainzer_netze.coordinator import MainzerNetzeCoordinator, fetch_window

from .fake_portal import FEATURE_ID, INDICATOR_ID
from .helpers import ENTRY_DATA, HA_FIXTURES, STATISTIC_ID, FakeClient, local, make_details

pytestmark = HA_FIXTURES

# --- fetch_window (pure) ---


def _feature(start: datetime | None, end: datetime | None) -> Feature:
    return Feature(FEATURE_ID, "x", "kWh", "QUARTER_HOUR", start, end)


def test_fetch_window_backfill_without_statistics() -> None:
    feature = _feature(local(2025, 7, 23), local(2026, 11, 13))
    assert fetch_window(feature, None) == (local(2025, 7, 23), local(2026, 11, 13))


def test_fetch_window_refetches_days_before_last_statistic() -> None:
    feature = _feature(local(2025, 7, 23), local(2026, 11, 14))
    last = local(2026, 11, 12, 23).astimezone(UTC)
    assert fetch_window(feature, last, refetch_days=2) == (local(2026, 11, 10), local(2026, 11, 14))


def test_fetch_window_refetch_across_dst_stays_on_local_midnight() -> None:
    feature = _feature(local(2025, 7, 23), local(2026, 10, 27))
    last = local(2026, 10, 26, 23).astimezone(UTC)
    start, _ = fetch_window(feature, last, refetch_days=2)
    assert start == local(2026, 10, 24)
    assert start.utcoffset() == timedelta(hours=2)  # still summer time on the 24th


def test_fetch_window_never_before_measured_start() -> None:
    feature = _feature(local(2026, 11, 12), local(2026, 11, 14))
    last = local(2026, 11, 12, 23).astimezone(UTC)
    assert fetch_window(feature, last, refetch_days=5)[0] == local(2026, 11, 12)


def test_fetch_window_none_without_data() -> None:
    assert fetch_window(_feature(None, None), None) is None
    assert fetch_window(_feature(local(2026, 11, 13), local(2026, 11, 13)), None) is None


# --- full update cycles ---


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="user@example.com")
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry, client: FakeClient, now: datetime):
    coordinator = MainzerNetzeCoordinator(hass, entry, client)  # type: ignore[arg-type]
    coordinator._now = lambda: now  # type: ignore[method-assign]
    return coordinator


async def _sums(hass: HomeAssistant) -> list[tuple[datetime, float]]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"sum"},
    )
    return [(datetime.fromtimestamp(r["start"], UTC), r["sum"]) for r in stats.get(STATISTIC_ID, [])]


def _assert_continuous(sums: list[tuple[datetime, float]]) -> None:
    for (prev_start, prev_sum), (start, value) in itertools.pairwise(sums):
        assert start - prev_start == timedelta(hours=1)
        assert value == pytest.approx(prev_sum + 1.0)  # 1 kWh per synthetic hour


async def test_backfill_then_incremental_update(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = FakeClient(make_details(local(2026, 10, 1), local(2026, 11, 13)))
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 13, 10))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert coordinator.last_update_success
    assert len(client.readings_calls) == 4  # 43 days in 14-day chunks
    sums = await _sums(hass)
    assert len(sums) == 43 * 24 + 1  # incl. the extra DST hour
    assert sums[-1][1] == pytest.approx(43 * 24 + 1)
    _assert_continuous(sums)

    state = coordinator.data[INDICATOR_ID].features[FEATURE_ID]
    assert state.last_day is not None and state.last_day.day.isoformat() == "2026-11-12"
    assert state.last_day.value == 24.0
    assert state.latest_reading_end == local(2026, 11, 13)
    assert client.logout_calls == 1

    # Next day: one new day available -> re-fetch window of 2 days + the new day.
    client.details = make_details(local(2026, 10, 1), local(2026, 11, 14))
    coordinator._now = lambda: local(2026, 11, 14, 10)  # type: ignore[method-assign]
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert client.readings_calls[-1] == (local(2026, 11, 10), local(2026, 11, 14))
    sums = await _sums(hass)
    assert len(sums) == 44 * 24 + 1
    _assert_continuous(sums)  # re-imported hours keep the cumulative sum consistent
    assert coordinator.data[INDICATOR_ID].features[FEATURE_ID].last_day.day.isoformat() == "2026-11-13"


async def test_no_portal_access_once_data_is_current(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = FakeClient()
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 13, 10))
    await coordinator.async_refresh()
    calls_after_first = len(client.calls)

    await coordinator.async_refresh()  # same day, measured_end == today's midnight
    assert len(client.calls) == calls_after_first
    assert coordinator.last_update_success


async def test_no_readings_fetch_without_new_data(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = FakeClient()
    # Portal still lags (data until 13.11. but it's already 14.11.) -> polls, but no new data.
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 14, 5))
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    fetches = len(client.readings_calls)

    await coordinator.async_refresh()
    assert client.calls.count("async_get_meter_details") == 2
    assert len(client.readings_calls) == fetches  # nothing new beyond the last imported hour


async def test_restart_fetches_refetch_window_for_sensor_values(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    client = FakeClient()
    await _coordinator(hass, entry, client, local(2026, 11, 14, 5)).async_refresh()
    await async_wait_recording_done(hass)

    # A fresh coordinator (HA restart) has no cached day total yet.
    restarted_client = FakeClient()
    restarted = _coordinator(hass, entry, restarted_client, local(2026, 11, 14, 5))
    await restarted.async_refresh()
    assert restarted_client.readings_calls == [(local(2026, 11, 10), local(2026, 11, 13))]
    assert restarted.data[INDICATOR_ID].features[FEATURE_ID].last_day.value == 24.0


async def test_interrupted_backfill_keeps_progress(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = FakeClient(make_details(local(2026, 10, 1), local(2026, 11, 13)))
    client.fail_on_readings_call = (3, MainzerNetzeConnectionError("boom"))
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 13, 10))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert not coordinator.last_update_success
    assert client.logout_calls == 1  # session closed even on failure
    partial = await _sums(hass)
    assert partial and partial[-1][0] < local(2026, 11, 13).astimezone(UTC)

    client.fail_on_readings_call = None
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert coordinator.last_update_success
    # resumed near the last imported hour instead of starting over
    assert client.readings_calls[-2][0] == local(2026, 10, 26)  # last hour 28.10. - 2 days
    sums = await _sums(hass)
    assert len(sums) == 43 * 24 + 1
    _assert_continuous(sums)


async def test_auth_error_starts_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = FakeClient()
    client.errors["async_get_meters"] = [MainzerNetzeAuthError("nope")]
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 13, 10))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


@pytest.mark.parametrize("error", [MainzerNetzeConnectionError("down"), MainzerNetzeApiError("weird")])
async def test_portal_errors_mark_update_failed(
    hass: HomeAssistant, entry: MockConfigEntry, error: Exception
) -> None:
    client = FakeClient()
    client.errors["async_get_meter_details"] = [error]
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 13, 10))
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert client.logout_calls == 1

    await coordinator.async_refresh()  # recovers on the next cycle
    assert coordinator.last_update_success


async def test_unsupported_unit_is_skipped(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    odd = Feature("WEIRD_FEATURE", "Odd", "furlongs", "QUARTER_HOUR", local(2026, 11, 1), local(2026, 11, 13))
    client = FakeClient(make_details(extra_features={"WEIRD_FEATURE": odd}))
    coordinator = _coordinator(hass, entry, client, local(2026, 11, 14, 5))
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert set(coordinator.data[INDICATOR_ID].features) == {FEATURE_ID}
    assert caplog.text.count("unsupported unit") == 1  # logged once, not every cycle


async def test_now_uses_portal_timezone(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    coordinator = MainzerNetzeCoordinator(hass, entry, FakeClient())  # type: ignore[arg-type]
    with patch("custom_components.mainzer_netze.coordinator.datetime") as mock_dt:
        mock_dt.now.return_value = local(2026, 11, 14)
        assert coordinator._now() == local(2026, 11, 14)
        mock_dt.now.assert_called_once_with(PORTAL_TZ)
