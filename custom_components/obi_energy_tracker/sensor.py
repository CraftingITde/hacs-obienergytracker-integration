"""Sensor platform for Obi EnergyTracker."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ObiEnergyTrackerConfigEntry
from .const import DOMAIN
from .coordinator import ObiEnergyTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ObiEnergyTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = config_entry.runtime_data

    sensors = [
        ObiMeterReadingSensor(coordinator),
        ObiFeedInReadingSensor(coordinator),
    ]

    async_add_entities(sensors)


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
    if not coordinator_data or not coordinator_data.get("meter"):
        return None

    meter_data = coordinator_data["meter"]
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


class ObiEnergySensorBase(CoordinatorEntity[ObiEnergyTrackerCoordinator], SensorEntity):
    """Base class for Obi EnergyTracker sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "obi_energy_tracker")},
            "name": "Obi EnergyTracker",
            "manufacturer": "Obi",
        }
        self._last_native_value: float | None = None
        self._last_native_value_set = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates and suppress duplicate readings."""
        new_value = self.native_value

        if not self._last_native_value_set or new_value != self._last_native_value:
            self._last_native_value_set = True
            self._last_native_value = new_value
            self.async_write_ha_state()


class ObiMeterReadingSensor(ObiEnergySensorBase):
    """Sensor for total meter reading (Zählerstand)."""

    _attr_unique_id = "obi_meter_reading"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "meter_reading"
    _attr_native_unit_of_measurement = "Wh"

    @property
    def native_value(self) -> float | None:
        """Return the meter reading value."""
        return _extract_meter_measure(
            self.coordinator.data, "energy", legacy_direct_key="energy"
        )


class ObiFeedInReadingSensor(ObiEnergySensorBase):
    """Sensor for total feed-in reading (Einspeisung)."""

    _attr_unique_id = "obi_feed_in_reading"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "feed_in_reading"
    _attr_native_unit_of_measurement = "Wh"

    @property
    def native_value(self) -> float | None:
        """Return the feed-in reading value."""
        return _extract_meter_measure(self.coordinator.data, "negative_energy")