"""Sensors for the Mainzer Netze integration.

The actual time series lives in the imported long-term statistics (see
statistics.py) -- use those in the Energy dashboard. The sensors here only
show summary values: the last complete day's total per series, how far the
portal's data reaches, and when the portal last imported data.

The day-total sensor deliberately has no state_class: HA would otherwise
build its own statistics from it and attribute yesterday's consumption to
the time the value arrived.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .coordinator import FeatureState, MainzerNetzeConfigEntry, MainzerNetzeCoordinator, MeterState
from .statistics import UNIT_MAP

PARALLEL_UPDATES = 0  # coordinator-based, no per-entity polling


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterState], datetime | None]


METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    MeterSensorDescription(
        key="data_until",
        translation_key="data_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m: max(
            (f.feature.measured_end for f in m.features.values() if f.feature.measured_end),
            default=None,
        ),
    ),
    MeterSensorDescription(
        key="last_import",
        translation_key="last_import",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.details.last_import,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MainzerNetzeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for indicator_id, meter_state in coordinator.data.items():
        entities.extend(MeterSensor(coordinator, indicator_id, description) for description in METER_SENSORS)
        entities.extend(
            LastDaySensor(coordinator, indicator_id, feature_id) for feature_id in meter_state.features
        )
    async_add_entities(entities)


class MainzerNetzeEntity(CoordinatorEntity[MainzerNetzeCoordinator]):
    """Base entity bound to one meter (one HA device per meter)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MainzerNetzeCoordinator, indicator_id: str) -> None:
        super().__init__(coordinator)
        self._indicator_id = indicator_id
        meter = coordinator.data[indicator_id].meter
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, indicator_id)},
            name=f"Mainzer Netze {meter.name}",
            manufacturer="Mainzer Netze",
            model=f"{meter.media_type.capitalize()} meter",
            serial_number=meter.location_no or None,
            configuration_url="https://msb.mainzer-netze.de/",
        )

    @property
    def meter_state(self) -> MeterState | None:
        return self.coordinator.data.get(self._indicator_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self.meter_state is not None


class MeterSensor(MainzerNetzeEntity, SensorEntity):
    entity_description: MeterSensorDescription

    def __init__(
        self,
        coordinator: MainzerNetzeCoordinator,
        indicator_id: str,
        description: MeterSensorDescription,
    ) -> None:
        super().__init__(coordinator, indicator_id)
        self.entity_description = description
        self._attr_unique_id = f"{indicator_id}_{description.key}"

    @property
    def native_value(self) -> datetime | None:
        state = self.meter_state
        return self.entity_description.value_fn(state) if state else None


class LastDaySensor(MainzerNetzeEntity, SensorEntity):
    """Total of the most recent complete day of one measurement series."""

    _attr_translation_key = "last_day"

    def __init__(self, coordinator: MainzerNetzeCoordinator, indicator_id: str, feature_id: str) -> None:
        super().__init__(coordinator, indicator_id)
        self._feature_id = feature_id
        feature = coordinator.data[indicator_id].features[feature_id].feature
        ha_unit, unit_class = UNIT_MAP[feature.unit]
        self._attr_unique_id = f"{indicator_id}_{feature_id}_last_day"
        self._attr_native_unit_of_measurement = ha_unit
        self._attr_device_class = (
            SensorDeviceClass.ENERGY if unit_class == EnergyConverter.UNIT_CLASS else None
        )
        self._attr_translation_placeholders = {"series": feature.display_name}

    @property
    def _feature_state(self) -> FeatureState | None:
        state = self.meter_state
        return state.features.get(self._feature_id) if state else None

    @property
    def native_value(self) -> float | None:
        state = self._feature_state
        return state.last_day.value if state and state.last_day else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self._feature_state
        if state is None:
            return None
        return {
            "date": state.last_day.day.isoformat() if state.last_day else None,
            "statistic_id": state.statistic_id,
        }
