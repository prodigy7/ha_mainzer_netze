"""Turning portal readings into Home Assistant long-term statistics.

The portal only delivers data with a delay of up to ~31 hours, so a normal
sensor state would attribute yesterday's consumption to "now". Instead,
readings are imported as external hourly statistics with their real
timestamps (same approach as HA's Tibber integration), which the Energy
dashboard can use directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import Reading
from .const import DOMAIN, PORTAL_TZ

# Portal display unit -> (HA unit, HA unit class). Features with other units
# are not imported (no safe conversion known).
UNIT_MAP: dict[str, tuple[str, str]] = {
    "Wh": (UnitOfEnergy.WATT_HOUR, EnergyConverter.UNIT_CLASS),
    "kWh": (UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS),
    "MWh": (UnitOfEnergy.MEGA_WATT_HOUR, EnergyConverter.UNIT_CLASS),
    "m³": (UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS),
}

# How far back to look for the cumulative sum preceding a re-import window.
_SUM_LOOKBACK = timedelta(days=31)
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class DayTotal:
    """Total of one complete local (portal time) day."""

    day: date
    value: float


def statistic_id_for(location_no: str, feature_id: str) -> str:
    """Builds a valid external statistic id, e.g. "mainzer_netze:de000..._power_active_...".

    Kept stable on purpose: changing it orphans the user's history.
    """
    object_id = f"{location_no}_{feature_id}".lower()
    object_id = "".join(c if c.isascii() and c.isalnum() else "_" for c in object_id)
    while "__" in object_id:
        object_id = object_id.replace("__", "_")
    return f"{DOMAIN}:{object_id.strip('_')}"


def _unique_utc(readings: list[Reading]) -> dict[tuple[datetime, datetime], float]:
    """Readings keyed by their UTC interval; duplicate intervals are counted once.

    UTC on purpose: equality and subtraction of datetimes sharing a ZoneInfo
    use wall-clock time and ignore `fold`, which would merge the repeated
    hour of the DST fall-back night and miscompute durations.
    """
    return {(r.start.astimezone(UTC), r.end.astimezone(UTC)): r.value for r in readings}


def aggregate_hourly(readings: list[Reading]) -> list[tuple[datetime, float]]:
    """Sums readings into UTC hour buckets, sorted by hour.

    German UTC offsets are whole hours, so local and UTC hour borders match.
    """
    buckets: dict[datetime, float] = {}
    for (start, _end), value in _unique_utc(readings).items():
        hour = start.replace(minute=0, second=0, microsecond=0)
        buckets[hour] = buckets.get(hour, 0.0) + value
    return [(hour, round(total, 6)) for hour, total in sorted(buckets.items())]


def last_complete_day(readings: list[Reading]) -> DayTotal | None:
    """Returns the most recent local day fully covered by readings.

    "Fully covered" means the readings' durations add up to the day's real
    length (23/24/25 h on DST days), so a partially delivered day is never
    reported as a day total.
    """
    coverage: dict[date, timedelta] = {}
    totals: dict[date, float] = {}
    for (start, end), value in _unique_utc(readings).items():
        day = start.astimezone(PORTAL_TZ).date()
        coverage[day] = coverage.get(day, timedelta()) + (end - start)
        totals[day] = totals.get(day, 0.0) + value

    for day in sorted(coverage, reverse=True):
        # Subtract in UTC: with the same ZoneInfo on both sides Python would
        # compute the wall-clock difference (always 24 h, ignoring DST).
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=PORTAL_TZ).astimezone(UTC)
        next_start = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), tzinfo=PORTAL_TZ
        ).astimezone(UTC)
        if coverage[day] >= next_start - day_start:
            return DayTotal(day=day, value=round(totals[day], 6))
    return None


def build_statistics(
    hourly: list[tuple[datetime, float]], base_sum: float
) -> tuple[list[StatisticData], float]:
    """Builds cumulative StatisticData rows; returns them and the final sum."""
    running = base_sum
    rows: list[StatisticData] = []
    for start, value in hourly:
        running = round(running + value, 6)
        rows.append(StatisticData(start=start, state=value, sum=running))
    return rows, running


async def async_get_last_statistic_start(hass: HomeAssistant, statistic_id: str) -> datetime | None:
    """Start of the newest imported hour, or None if nothing was imported yet."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, False, {"sum"}
    )
    rows = result.get(statistic_id)
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"])


async def async_get_sum_before(hass: HomeAssistant, statistic_id: str, before: datetime) -> float:
    """Cumulative sum of the last imported hour that starts before `before` (0.0 if none)."""
    for lookback_start in (before - _SUM_LOOKBACK, _EPOCH):
        result = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            lookback_start,
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = result.get(statistic_id)
        if rows:
            return float(cast(float, rows[-1].get("sum") or 0.0))
    return 0.0


def async_import_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    unit: str,
    rows: list[StatisticData],
) -> None:
    """Queues the rows for import into the recorder (replaces existing hours)."""
    if not rows:
        return
    ha_unit, unit_class = UNIT_MAP[unit]
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=unit_class,
        unit_of_measurement=ha_unit,
    )
    async_add_external_statistics(hass, metadata, rows)
