"""Sensor platform for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ObiEnergyTrackerConfigEntry
from .const import DATA_METER, MEASURE_ENERGY, MEASURE_NEGATIVE_ENERGY
from .coordinator import ObiEnergyTrackerCoordinator
from .entity import ObiEnergyTrackerEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ObiEnergyTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = config_entry.runtime_data

    async_add_entities(
        [
            ObiMeterReadingSensor(coordinator),
            ObiFeedInReadingSensor(coordinator),
            ObiBatterySensor(coordinator),
            ObiConnectionStrengthSensor(coordinator),
            ObiLastRecordSensor(coordinator),
            ObiOtaStatusSensor(coordinator),
        ]
    )


def _extract_meter_measure(
    coordinator_data: dict[str, Any] | None,
    measure: str,
    *,
    legacy_direct_key: str | None = None,
) -> float | None:
    """Extract the latest value for a measure from meter data.

    The meter endpoint returns either a single record or a list of records.
    Records are tagged with a "measure" field once multiple measures are
    requested together; older single-measure responses may expose the value
    directly under the measure's own key instead.
    """
    if not coordinator_data or not coordinator_data.get(DATA_METER):
        return None

    meter_data = coordinator_data[DATA_METER]
    records = meter_data if isinstance(meter_data, list) else [meter_data]
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return None

    matching = [r for r in records if r.get("measure") == measure]
    if matching:
        record = matching[-1]
        return record.get("value")

    if legacy_direct_key:
        record = records[-1]
        if legacy_direct_key in record:
            return record[legacy_direct_key]
        if "value" in record and record.get("measure") is None:
            return record["value"]

    return None


class ObiEnergySensorBase(ObiEnergyTrackerEntity, SensorEntity):
    """Base class for Obi EnergyTracker sensors."""

    def _state_fingerprint(self) -> tuple[Any, ...]:
        """Include the reported value in the change detection."""
        return (self.available, self.native_value)


class ObiMeterReadingSensor(ObiEnergySensorBase):
    """Sensor for the total meter reading (Zählerstand)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "meter_reading")

    @property
    def native_value(self) -> float | None:
        """Return the meter reading value."""
        return _extract_meter_measure(
            self.coordinator.data, MEASURE_ENERGY, legacy_direct_key=MEASURE_ENERGY
        )


class ObiFeedInReadingSensor(ObiEnergySensorBase):
    """Sensor for the total feed-in reading (Einspeisung)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "feed_in_reading")

    @property
    def native_value(self) -> float | None:
        """Return the feed-in reading value."""
        return _extract_meter_measure(self.coordinator.data, MEASURE_NEGATIVE_ENERGY)


class ObiBatterySensor(ObiEnergySensorBase):
    """Battery level of the meter sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "battery")

    @property
    def native_value(self) -> int | None:
        """Return the battery level in percent."""
        value = self.coordinator.sensor_data.get("batteryLevel")
        return value if isinstance(value, int) else None


class ObiConnectionStrengthSensor(ObiEnergySensorBase):
    """Reported connection quality between sensor and bridge."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "connection_strength")

    @property
    def native_value(self) -> str | None:
        """Return the connection strength as reported by the backend."""
        value = self.coordinator.sensor_data.get("connectionStrength")
        return value if isinstance(value, str) else None


class ObiLastRecordSensor(ObiEnergySensorBase):
    """Timestamp of the last reading the backend received."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "last_record")

    @property
    def native_value(self) -> datetime | None:
        """Return when the backend last received a reading."""
        raw = self.coordinator.sensor_data.get("lastRecordReceivedAt")
        if not isinstance(raw, str):
            return None
        return dt_util.parse_datetime(raw)


class ObiOtaStatusSensor(ObiEnergySensorBase):
    """Firmware update status reported by the sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "ota_status")

    @property
    def native_value(self) -> str | None:
        """Return the OTA status."""
        value = self.coordinator.sensor_data.get("otaStatus")
        return value if isinstance(value, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the update progress, if the backend reports one."""
        return {"progress": self.coordinator.sensor_data.get("otaProgress")}

    def _state_fingerprint(self) -> tuple[Any, ...]:
        """Include the progress so it keeps ticking during an update.

        The status stays on the same value for the whole update, so without the
        progress in the comparison the attribute would freeze at its first value.
        """
        progress = self.coordinator.sensor_data.get("otaProgress")
        return (*super()._state_fingerprint(), progress)
