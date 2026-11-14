"""DataUpdateCoordinator for the Mainzer Netze integration.

One update cycle: (skip if yesterday's data is already in) -> login ->
per meter: initData -> per feature: fetch new quarter-hours (if the portal has
any beyond our last imported hour) -> import hourly statistics -> logout.

The portal delivers data once a day, covering up to the previous midnight, so
after the morning import every further hourly tick is a no-op without network
access. That keeps the load on the portal at a few small requests per day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Feature,
    MainzerNetzeAuthError,
    MainzerNetzeClient,
    MainzerNetzeError,
    Meter,
    MeterDetails,
    split_range,
)
from .const import DOMAIN, PORTAL_TZ, REFETCH_DAYS, UPDATE_INTERVAL
from .statistics import (
    UNIT_MAP,
    DayTotal,
    aggregate_hourly,
    async_get_last_statistic_start,
    async_get_sum_before,
    async_import_statistics,
    build_statistics,
    last_complete_day,
    statistic_id_for,
)

_LOGGER = logging.getLogger(__name__)

type MainzerNetzeConfigEntry = ConfigEntry[MainzerNetzeCoordinator]


@dataclass(frozen=True)
class FeatureState:
    """Latest known state of one measurement series."""

    feature: Feature
    statistic_id: str
    last_day: DayTotal | None = None
    latest_reading_end: datetime | None = None


@dataclass(frozen=True)
class MeterState:
    meter: Meter
    details: MeterDetails
    features: dict[str, FeatureState] = field(default_factory=dict)


type MainzerNetzeData = dict[str, MeterState]  # keyed by indicator_id


def local_midnight(value: datetime) -> datetime:
    """Local (portal time) midnight of the day `value` falls on."""
    return datetime.combine(value.astimezone(PORTAL_TZ).date(), datetime.min.time(), tzinfo=PORTAL_TZ)


def fetch_window(
    feature: Feature, last_statistic_start: datetime | None, refetch_days: int = REFETCH_DAYS
) -> tuple[datetime, datetime] | None:
    """Range [start, end) to fetch for a feature, or None if there's nothing to fetch.

    Without imported statistics this is the full measured history (backfill).
    Otherwise it starts `refetch_days` before the last imported hour (aligned
    to local midnight) to pick up late corrections by the operator.
    """
    if feature.measured_start is None or feature.measured_end is None:
        return None
    start = feature.measured_start
    if last_statistic_start is not None:
        # Wall-clock arithmetic on a ZoneInfo datetime: stays at local
        # midnight with the correct offset even across a DST switch.
        refetch_from = local_midnight(last_statistic_start) - timedelta(days=refetch_days)
        start = max(start, refetch_from)
    if start >= feature.measured_end:
        return None
    return start, feature.measured_end


class MainzerNetzeCoordinator(DataUpdateCoordinator[MainzerNetzeData]):
    """Fetches portal data and keeps the statistics up to date."""

    config_entry: MainzerNetzeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: MainzerNetzeConfigEntry, client: MainzerNetzeClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._meters: list[Meter] | None = None
        self._unsupported_units_logged: set[str] = set()

    def _now(self) -> datetime:
        return datetime.now(PORTAL_TZ)

    def _is_data_current(self) -> bool:
        """True if every series already has data up to today's midnight (nothing new possible)."""
        if not self.data:
            return False
        today = local_midnight(self._now())
        states = [s for meter in self.data.values() for s in meter.features.values()]
        return bool(states) and all(
            s.feature.measured_end is not None and s.feature.measured_end >= today for s in states
        )

    async def _async_update_data(self) -> MainzerNetzeData:
        if self._is_data_current():
            _LOGGER.debug("All data up to today is imported, skipping portal access")
            return self.data

        try:
            if self._meters is None:
                self._meters = await self.client.async_get_meters()
            result: MainzerNetzeData = {}
            for meter in self._meters:
                details = await self.client.async_get_meter_details(meter.indicator_id)
                result[meter.indicator_id] = MeterState(
                    meter=meter,
                    details=details,
                    features=await self._async_update_features(meter, details),
                )
            return result
        except MainzerNetzeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MainzerNetzeError as err:
            raise UpdateFailed(f"Error fetching data from the Mainzer Netze portal: {err}") from err
        finally:
            await self.client.async_logout()

    async def _async_update_features(self, meter: Meter, details: MeterDetails) -> dict[str, FeatureState]:
        previous = self.data.get(meter.indicator_id) if self.data else None
        states: dict[str, FeatureState] = {}
        for feature in details.features.values():
            if feature.unit not in UNIT_MAP:
                if feature.feature_id not in self._unsupported_units_logged:
                    self._unsupported_units_logged.add(feature.feature_id)
                    _LOGGER.warning(
                        "Skipping series %s of meter %s: unsupported unit %r",
                        feature.feature_id,
                        meter.location_no,
                        feature.unit,
                    )
                continue
            prev_state = previous.features.get(feature.feature_id) if previous else None
            states[feature.feature_id] = await self._async_update_feature(details, feature, prev_state)
        return states

    async def _async_update_feature(
        self, details: MeterDetails, feature: Feature, previous: FeatureState | None
    ) -> FeatureState:
        statistic_id = statistic_id_for(details.location_no or details.indicator_id, feature.feature_id)
        state = (
            replace(previous, feature=feature)
            if previous
            else FeatureState(feature=feature, statistic_id=statistic_id)
        )

        last_start = await async_get_last_statistic_start(self.hass, statistic_id)
        has_new_data = (
            last_start is None
            or feature.measured_end is None
            or feature.measured_end > last_start + timedelta(hours=1)
        )
        # Without a cached day total (first run after HA start), fetch the
        # refetch window once anyway so the sensors have values.
        if not has_new_data and state.last_day is not None:
            return state

        window = fetch_window(feature, last_start)
        if window is None:
            return state

        _LOGGER.debug("Fetching %s %s from %s to %s", statistic_id, feature.unit, *window)
        name = f"{details.name} {feature.display_name}"
        running_sum: float | None = None
        last_day, latest_end = state.last_day, state.latest_reading_end
        # Chunk by chunk, importing each right away: an interrupted backfill
        # keeps its progress and the next cycle resumes from there.
        for chunk_start, chunk_end in split_range(*window):
            readings = await self.client.async_get_readings(
                details, feature.feature_id, chunk_start, chunk_end
            )
            if not readings:
                continue
            if running_sum is None:
                running_sum = await async_get_sum_before(self.hass, statistic_id, window[0])
            rows, running_sum = build_statistics(aggregate_hourly(readings), running_sum)
            async_import_statistics(self.hass, statistic_id, name, feature.unit, rows)
            last_day = last_complete_day(readings) or last_day
            chunk_latest = max(r.end for r in readings)
            latest_end = chunk_latest if latest_end is None else max(latest_end, chunk_latest)

        return replace(state, last_day=last_day, latest_reading_end=latest_end)
