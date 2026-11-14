"""Tests for statistics.py (pure aggregation + recorder round trip)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period, valid_statistic_id
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.mainzer_netze.api import Reading, parse_readings
from custom_components.mainzer_netze.statistics import (
    DayTotal,
    aggregate_hourly,
    async_get_last_statistic_start,
    async_get_sum_before,
    async_import_statistics,
    build_statistics,
    last_complete_day,
    statistic_id_for,
)

from .fake_portal import FEATURE_ID, INDICATOR_ID
from .helpers import HA_FIXTURES, LOCATION_NO, STATISTIC_ID, local, make_readings

pytestmark = HA_FIXTURES


def test_statistic_id_is_valid_and_stable() -> None:
    statistic_id = statistic_id_for(LOCATION_NO, FEATURE_ID)
    assert statistic_id == STATISTIC_ID
    assert valid_statistic_id(statistic_id)


@pytest.mark.parametrize("location_no", ["DE-00 12/34", "__x__", "Ä-Ö"])
def test_statistic_id_sanitizes_odd_input(location_no: str) -> None:
    assert valid_statistic_id(statistic_id_for(location_no, FEATURE_ID))


def test_aggregate_hourly_real_day(values_day: dict[str, Any]) -> None:
    readings = parse_readings(values_day, INDICATOR_ID, FEATURE_ID)
    hourly = aggregate_hourly(readings)
    assert len(hourly) == 24
    assert hourly[0][0] == datetime(2026, 11, 11, 23, tzinfo=UTC)  # 00:00 local (+01:00)
    assert all(start.tzinfo is UTC for start, _ in hourly)
    assert round(sum(v for _, v in hourly), 3) == 12.132


def test_aggregate_hourly_real_dst_switch(values_dst: dict[str, Any]) -> None:
    readings = parse_readings(values_dst, INDICATOR_ID, FEATURE_ID)
    hourly = aggregate_hourly(readings)
    assert len(hourly) == 24 + 25  # both local 02:00-03:00 hours kept apart in UTC
    hours = [h for h, _ in hourly]
    assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(hours))
    assert sum(v for _, v in hourly) == pytest.approx(sum(r.value for r in readings))


def test_aggregate_hourly_counts_duplicates_once() -> None:
    readings = make_readings(local(2026, 11, 12, 0), local(2026, 11, 12, 1))
    assert aggregate_hourly(readings + readings) == [(datetime(2026, 11, 11, 23, tzinfo=UTC), 1.0)]


def test_last_complete_day_skips_partial_latest_day() -> None:
    readings = make_readings(local(2026, 11, 11), local(2026, 11, 13, 6))
    assert last_complete_day(readings) == DayTotal(day=date(2026, 11, 12), value=24.0)


def test_last_complete_day_dst_fall_back_has_25_hours() -> None:
    readings = make_readings(local(2026, 10, 25), local(2026, 10, 26))
    assert last_complete_day(readings) == DayTotal(day=date(2026, 10, 25), value=25.0)


def test_last_complete_day_dst_spring_forward_has_23_hours() -> None:
    # Regression: same-ZoneInfo datetime subtraction yields 24 h for this day.
    readings = make_readings(local(2026, 3, 29), local(2026, 3, 30))
    assert last_complete_day(readings) == DayTotal(day=date(2026, 3, 29), value=23.0)


def test_last_complete_day_none_without_full_day() -> None:
    assert last_complete_day(make_readings(local(2026, 11, 12, 1), local(2026, 11, 13))) is None
    assert last_complete_day([]) is None


def test_build_statistics_running_sum() -> None:
    hourly = [(datetime(2026, 11, 12, h, tzinfo=UTC), 0.5) for h in range(3)]
    rows, final = build_statistics(hourly, base_sum=10.0)
    assert [r["sum"] for r in rows] == [10.5, 11.0, 11.5]
    assert [r["state"] for r in rows] == [0.5, 0.5, 0.5]
    assert final == 11.5


async def test_recorder_round_trip(hass: HomeAssistant) -> None:
    assert await async_get_last_statistic_start(hass, STATISTIC_ID) is None
    assert await async_get_sum_before(hass, STATISTIC_ID, datetime(2026, 11, 12, tzinfo=UTC)) == 0.0

    hourly = aggregate_hourly(make_readings(local(2026, 11, 12), local(2026, 11, 13)))
    rows, _ = build_statistics(hourly, 0.0)
    async_import_statistics(hass, STATISTIC_ID, "Test", "kWh", rows)
    await async_wait_recording_done(hass)

    last_start = await async_get_last_statistic_start(hass, STATISTIC_ID)
    assert last_start == datetime(2026, 11, 12, 22, tzinfo=UTC)  # 23:00 local
    before = local(2026, 11, 12, 12).astimezone(UTC)
    assert await async_get_sum_before(hass, STATISTIC_ID, before) == 12.0

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, hourly[0][0], None, {STATISTIC_ID}, "hour", None, {"sum"}
    )
    assert len(stats[STATISTIC_ID]) == 24


async def test_import_without_rows_is_noop(hass: HomeAssistant) -> None:
    async_import_statistics(hass, STATISTIC_ID, "Test", "kWh", [])
    await async_wait_recording_done(hass)
    assert await async_get_last_statistic_start(hass, STATISTIC_ID) is None


def _reading(start: datetime, minutes: int, value: float) -> Reading:
    return Reading(start=start, end=start + timedelta(minutes=minutes), value=value)


def test_aggregate_hourly_native_hour_granularity() -> None:
    start = datetime(2026, 11, 12, 5, tzinfo=UTC)
    assert aggregate_hourly([_reading(start, 60, 2.0)]) == [(start, 2.0)]
