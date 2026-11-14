"""Fakes shared by the HA-level tests.

FakeClient replaces MainzerNetzeClient above the HTTP layer (the HTTP layer
itself is covered by test_api_client.py against the fake portal).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.mainzer_netze.api import (
    Feature,
    MainzerNetzeError,
    Meter,
    MeterDetails,
    Reading,
)
from custom_components.mainzer_netze.const import CONF_PASSWORD, CONF_USERNAME, PORTAL_TZ

from .fake_portal import FEATURE_ID, INDICATOR_ID, PASSWORD, USERNAME

# For HA-level test modules: `pytestmark = HA_FIXTURES`. Order matters, the
# recorder has to be set up before `hass`.
HA_FIXTURES = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

LOCATION_NO = "DE0000000000000000000000000000001"
STATISTIC_ID = f"mainzer_netze:{LOCATION_NO.lower()}_{FEATURE_ID.lower()}"
ENTRY_DATA = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
# Every synthetic quarter-hour is 0.25 kWh -> 1 kWh per hour, 24 kWh per normal day.
QUARTER_HOUR_VALUE = 0.25


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=PORTAL_TZ)


def make_meter() -> Meter:
    return Meter(
        indicator_id=INDICATOR_ID, name="Example Customer", location_no=LOCATION_NO, media_type="POWER"
    )


DEFAULT_MEASURED_START = local(2026, 11, 1)
DEFAULT_MEASURED_END = local(2026, 11, 13)


def make_details(
    measured_start: datetime = DEFAULT_MEASURED_START,
    measured_end: datetime = DEFAULT_MEASURED_END,
    unit: str = "kWh",
    extra_features: dict[str, Feature] | None = None,
) -> MeterDetails:
    features = {
        FEATURE_ID: Feature(
            feature_id=FEATURE_ID,
            display_name="(+) Elektrizität: Wirkenergie",
            unit=unit,
            native_granularity="QUARTER_HOUR",
            measured_start=measured_start,
            measured_end=measured_end,
        ),
        **(extra_features or {}),
    }
    return MeterDetails(
        indicator_id=INDICATOR_ID,
        indicator_type="METER",
        name="Example Customer",
        location_no=LOCATION_NO,
        last_import=measured_end + timedelta(hours=7),
        features=features,
    )


def make_readings(start: datetime, end: datetime, value: float = QUARTER_HOUR_VALUE) -> list[Reading]:
    """Contiguous quarter-hour readings for [start, end) in absolute time."""
    readings = []
    cursor = start.astimezone(UTC)
    while cursor < end.astimezone(UTC):
        nxt = cursor + timedelta(minutes=15)
        readings.append(
            Reading(start=cursor.astimezone(PORTAL_TZ), end=nxt.astimezone(PORTAL_TZ), value=value)
        )
        cursor = nxt
    return readings


class FakeClient:
    """Duck-typed stand-in for MainzerNetzeClient."""

    def __init__(self, details: MeterDetails | None = None) -> None:
        self.details = details or make_details()
        self.meters = [make_meter()]
        self.readings_calls: list[tuple[datetime, datetime]] = []
        self.calls: list[str] = []
        self.logout_calls = 0
        # Exceptions to raise, keyed by method name; each raised once (FIFO).
        self.errors: dict[str, list[Exception]] = {}
        # Raise this on the n-th (1-based) readings call, to simulate an interrupted backfill.
        self.fail_on_readings_call: tuple[int, MainzerNetzeError] | None = None

    def _maybe_raise(self, name: str) -> None:
        self.calls.append(name)
        if self.errors.get(name):
            raise self.errors[name].pop(0)

    async def async_login(self) -> None:
        self._maybe_raise("async_login")

    async def async_get_meters(self) -> list[Meter]:
        self._maybe_raise("async_get_meters")
        return self.meters

    async def async_get_meter_details(self, indicator_id: str) -> MeterDetails:
        self._maybe_raise("async_get_meter_details")
        return self.details

    async def async_get_readings(
        self, details: MeterDetails, feature_id: str, start: datetime, end: datetime
    ) -> list[Reading]:
        self._maybe_raise("async_get_readings")
        self.readings_calls.append((start, end))
        if self.fail_on_readings_call and len(self.readings_calls) == self.fail_on_readings_call[0]:
            raise self.fail_on_readings_call[1]
        return make_readings(start, end)

    async def async_logout(self) -> None:
        self.logout_calls += 1
