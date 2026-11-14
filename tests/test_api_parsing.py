"""Tests for the pure parsing/request-building helpers in api.py (real portal fixtures)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.mainzer_netze.api import (
    MainzerNetzeApiError,
    build_value_request,
    format_portal_datetime,
    parse_meter_details,
    parse_meters,
    parse_readings,
    split_range,
)
from custom_components.mainzer_netze.const import PORTAL_TZ

from .fake_portal import FEATURE_ID, INDICATOR_ID


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=PORTAL_TZ)


# --- parse_meters ---


def test_parse_meters_real_tree(tree: dict[str, Any]) -> None:
    meters = parse_meters(tree)
    assert len(meters) == 1
    assert meters[0].indicator_id == INDICATOR_ID
    assert meters[0].name == "Example Customer"
    assert meters[0].location_no == "DE0000000000000000000000000000001"
    assert meters[0].media_type == "POWER"


def test_parse_meters_nested_groups_are_walked_but_not_returned() -> None:
    tree = {
        "type": "rootIndicatorGroup",
        "children": [
            {
                "type": "indicatorGroup",
                "id": "g1",
                "children": [
                    {"type": "indicatorPOWER", "id": "m1", "text": "A", "children": []},
                    {"type": "indicatorGAS", "id": "m2", "text": "B"},
                ],
            }
        ],
    }
    assert [(m.indicator_id, m.media_type) for m in parse_meters(tree)] == [
        ("m1", "POWER"),
        ("m2", "GAS"),
    ]


@pytest.mark.parametrize("bad", [{}, {"exception": "x"}, [], None, {"children": "nope"}])
def test_parse_meters_invalid_shape_is_api_error(bad: Any) -> None:
    with pytest.raises(MainzerNetzeApiError):
        parse_meters(bad)


# --- parse_meter_details ---


def test_parse_meter_details_real(init_data: dict[str, Any]) -> None:
    details = parse_meter_details(init_data, INDICATOR_ID)
    assert details.indicator_type == "METER"
    assert details.last_import == datetime.fromisoformat("2026-11-13T07:08:59.571+01:00")
    feature = details.features[FEATURE_ID]
    assert feature.unit == "kWh"
    assert feature.display_name == "(+) Elektrizität: Wirkenergie"
    assert feature.native_granularity == "QUARTER_HOUR"
    assert feature.measured_start == _local(2025, 7, 23)
    assert feature.measured_end == _local(2026, 11, 13)


def test_parse_meter_details_expired_session_empty_object() -> None:
    # Live-verified: initData answers HTTP 200 "{}" without a valid session.
    with pytest.raises(MainzerNetzeApiError):
        parse_meter_details({}, INDICATOR_ID)


def test_parse_meter_details_other_indicator_rejected(init_data: dict[str, Any]) -> None:
    with pytest.raises(MainzerNetzeApiError):
        parse_meter_details(init_data, "some-other-id")


def test_parse_meter_details_tolerates_missing_optional_fields() -> None:
    details = parse_meter_details({"currentIndicator": {"indicatorID": "x"}}, "x")
    assert details.features == {}
    assert details.last_import is None
    assert details.name == "x"


# --- parse_readings ---


def test_parse_readings_real_day(values_day: dict[str, Any]) -> None:
    readings = parse_readings(values_day, INDICATOR_ID, FEATURE_ID)
    assert len(readings) == 96
    assert readings[0].start == _local(2026, 11, 12)
    assert readings[-1].end == _local(2026, 11, 13)
    assert round(sum(r.value for r in readings), 3) == 12.132


def test_parse_readings_real_dst_day(values_dst: dict[str, Any]) -> None:
    readings = parse_readings(values_dst, INDICATOR_ID, FEATURE_ID)
    assert len(readings) == 96 + 100  # 2026-10-25 has 25 hours
    # strictly contiguous in absolute time, despite the repeated local 02:00-03:00
    for prev, cur in itertools.pairwise(readings):
        assert prev.end == cur.start
        assert cur.end - cur.start == timedelta(minutes=15)


def test_parse_readings_skips_null_negative_and_malformed() -> None:
    period = {"2026-11-12T00:00:00.000+01:00": "2026-11-12T00:15:00.000+01:00"}
    values = [
        {"value": None, "measuringPeriod": period},
        {"value": -1.0, "measuringPeriod": period},
        {"value": "1.0", "measuringPeriod": period},
        {"value": True, "measuringPeriod": period},
        {"value": float("nan"), "measuringPeriod": period},
        {"value": 1.0, "measuringPeriod": {"2026-11-12T00:00:00": "2026-11-12T00:15:00"}},  # naive
        {"value": 1.0, "measuringPeriod": {"2026-11-12T00:15:00.000+01:00": "2026-11-12T00:00:00.000+01:00"}},
        {"value": 1.0},
        "garbage",
        {"value": 0.5, "measuringPeriod": period},
    ]
    data = {
        "indicatorValueResponses": [
            {
                "indicatorID": INDICATOR_ID,
                "featureValueResponses": [{"featureID": FEATURE_ID, "values": values}],
            }
        ]
    }
    readings = parse_readings(data, INDICATOR_ID, FEATURE_ID)
    assert [r.value for r in readings] == [0.5]


def test_parse_readings_ignores_other_indicators_and_features(values_day: dict[str, Any]) -> None:
    assert parse_readings(values_day, "other", FEATURE_ID) == []
    assert parse_readings(values_day, INDICATOR_ID, "OTHER_FEATURE") == []


def test_parse_readings_empty_response_is_no_data() -> None:
    assert parse_readings({"indicatorValueResponses": []}, INDICATOR_ID, FEATURE_ID) == []


@pytest.mark.parametrize("bad", [{}, {"exception": "x"}, None, "html"])
def test_parse_readings_invalid_shape_is_api_error(bad: Any) -> None:
    with pytest.raises(MainzerNetzeApiError):
        parse_readings(bad, INDICATOR_ID, FEATURE_ID)


# --- split_range ---


def test_split_range_single_chunk() -> None:
    start, end = _local(2026, 11, 1), _local(2026, 11, 13)
    assert split_range(start, end, 14) == [(start, end)]


def test_split_range_chunks_on_local_midnight_across_dst() -> None:
    start, end = _local(2026, 10, 1), _local(2026, 11, 13)
    chunks = split_range(start, end, 14)
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (_, prev_end), (next_start, _) in itertools.pairwise(chunks):
        assert prev_end == next_start
        local = prev_end.astimezone(PORTAL_TZ)
        assert (local.hour, local.minute) == (0, 0)
    assert [c[1].astimezone(PORTAL_TZ).date().isoformat() for c in chunks] == [
        "2026-10-15",
        "2026-10-29",
        "2026-11-12",
        "2026-11-13",
    ]


def test_split_range_empty_and_invalid() -> None:
    start = _local(2026, 11, 1)
    assert split_range(start, start) == []
    with pytest.raises(ValueError):
        split_range(start, start + timedelta(days=1), 0)


# --- request building ---


def test_format_portal_datetime_matches_frontend_format() -> None:
    assert format_portal_datetime(_local(2026, 11, 12)) == "2026-11-12T00:00:00.000+01:00"
    assert format_portal_datetime(_local(2026, 7, 1)) == "2026-07-01T00:00:00.000+02:00"


def test_build_value_request_matches_live_verified_payload(init_data: dict[str, Any]) -> None:
    details = parse_meter_details(init_data, INDICATOR_ID)
    payload = build_value_request(details, FEATURE_ID, _local(2026, 11, 12), _local(2026, 11, 13))
    assert payload == {
        "dateType": "INDIVIDUAL",
        "userTimeZone": "Europe/Berlin",
        "indicatorValueRequests": [
            {
                "valueRange": {"2026-11-12T00:00:00.000+01:00": "2026-11-13T00:00:00.000+01:00"},
                "indicatorID": INDICATOR_ID,
                "indicatorType": "METER",
                "index": 0,
                "featureValueRequests": [
                    {"featureID": FEATURE_ID, "granularityType": "QUARTER_HOUR", "aggregationType": "SUM"}
                ],
            }
        ],
    }
